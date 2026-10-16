"""Explicit hardware hierarchy: Cabinet -> CIOC -> Baseplate -> CHARM slot.

Single source of truth for geometry and naming. Positions inside a CIOC are
addressed by a linear *offset* (baseplate * charms_per_baseplate + slot).
"""
from __future__ import annotations

from .models import HardwareConfig, NamingConfig, Placement


class Topology:
    def __init__(self, hw: HardwareConfig, naming: NamingConfig):
        self.hw = hw
        self.naming = naming

    # geometry -------------------------------------------------------------
    @property
    def charms(self) -> int:
        return self.hw.charms_per_baseplate

    @property
    def slots_per_cioc(self) -> int:
        return self.hw.slots_per_cioc

    @property
    def slots_per_cabinet(self) -> int:
        return self.hw.slots_per_cabinet

    def offset(self, p: Placement) -> int:
        return p.bp * self.charms + p.slot

    def split_offset(self, off: int) -> tuple[int, int]:
        return divmod(off, self.charms)

    def in_bounds(self, p: Placement) -> bool:
        return (p.cab >= 0 and 0 <= p.cioc < self.hw.ciocs_per_cabinet
                and 0 <= p.bp < self.hw.baseplates_per_cioc
                and 0 <= p.slot < self.hw.charms_per_baseplate)

    def cabinet_baseplate_index(self, cioc: int, bp: int) -> int:
        return cioc * self.hw.baseplates_per_cioc + bp

    # reserved (spare) baseplates are the *tail* of each cabinet ------------
    def usable_baseplates(self, cioc: int, reserved: int) -> int:
        """Usable baseplates of CIOC ``cioc`` when ``reserved`` baseplates per
        cabinet are kept empty, taken from the highest-numbered CIOCs first."""
        per = self.hw.baseplates_per_cioc
        n = self.hw.ciocs_per_cabinet
        remaining = reserved
        lost = [0] * n
        for c in range(n - 1, -1, -1):
            take = min(per, remaining)
            lost[c] = take
            remaining -= take
        return per - lost[cioc]

    # names ------------------------------------------------------------------
    def _fmt(self, pat: str, cab=0, cioc=0, bp=0, slot=0) -> str:
        return pat.format(cab=cab + 1, cioc=cioc + 1, bp=bp + 1, slot=slot + 1)

    def system_cabinet(self, cab: int) -> str:
        return self._fmt(self.naming.system_cabinet, cab=cab)

    def marshalling_cabinet(self, cab: int) -> str:
        return self._fmt(self.naming.marshalling_cabinet, cab=cab)

    def controller(self, cab: int, cioc: int) -> str:
        return self._fmt(self.naming.controller, cab=cab, cioc=cioc)

    def cioc_name(self, cab: int, cioc: int) -> str:
        return self._fmt(self.naming.cioc, cab=cab, cioc=cioc)

    def baseplate_name(self, cab: int, cioc: int, bp: int) -> str:
        return self._fmt(self.naming.baseplate, cab=cab, cioc=cioc, bp=bp)

    def describe(self, p: Placement) -> dict:
        return {
            "Cabinet": self.system_cabinet(p.cab),
            "Marshalling Cabinet": self.marshalling_cabinet(p.cab),
            "Controller": self.controller(p.cab, p.cioc),
            "CIOC": self.cioc_name(p.cab, p.cioc),
            "Baseplate": self.baseplate_name(p.cab, p.cioc, p.bp),
            "CHARM Slot": p.slot + 1,
        }

    def location_str(self, p: Placement) -> str:
        return f"{self.baseplate_name(p.cab, p.cioc, p.bp)}/slot {p.slot + 1}"

    def names_unique(self, n_cabinets: int) -> list[str]:
        """Return a list of duplicated names (empty when naming is unique)."""
        dup: list[str] = []
        seen: dict[str, str] = {}
        for cab in range(max(1, n_cabinets)):
            for kind, name in [("cabinet", self.system_cabinet(cab)),
                               ("marshalling", self.marshalling_cabinet(cab))]:
                if (kind, name) in seen:
                    dup.append(f"{kind}:{name}")
                seen[(kind, name)] = name
            for c in range(self.hw.ciocs_per_cabinet):
                for kind, name in [("controller", self.controller(cab, c)),
                                   ("cioc", self.cioc_name(cab, c))]:
                    if (kind, name) in seen:
                        dup.append(f"{kind}:{name}")
                    seen[(kind, name)] = name
                for b in range(self.hw.baseplates_per_cioc):
                    name = self.baseplate_name(cab, c, b)
                    if ("bp", name) in seen:
                        dup.append(f"bp:{name}")
                    seen[("bp", name)] = name
        return dup
