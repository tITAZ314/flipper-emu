"""Turn a Renode run log into a bring-up report.

The development loop for this project is: run the firmware, read what the
emulator complained about, implement the peripheral it wanted, repeat.  This
module performs the "read what it complained about" half — it pulls unhandled
memory accesses, CPU faults and fatal errors out of a Renode log and groups the
accesses by the peripheral they belong to, using the same address map that
generated the platform.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..fw import memmap

#: Renode reports accesses to unmapped addresses in two wordings: one per access
#: ("ReadDoubleWord from non existing peripheral at 0x...") and one for a whole
#: range. Both are matched, and the access width is folded into "read"/"write".
UNHANDLED_RE = re.compile(
    r"(?i)(read|write)[a-z]*\s+(?:from|to)\s+non[- ]existing peripheral at 0x([0-9a-f]{4,8})"
)
UNHANDLED_RANGE_RE = re.compile(
    r"(?i)non[- ]existing peripheral in range <0x([0-9a-f]{4,8}),"
)
#: Any other address-bearing complaint (e.g. "no peripheral at ...").
ADDRESS_RE = re.compile(r"(?i)0x([0-9a-f]{8})")
#: Problems inside our own peripheral models or probe scripts (IronPython errors,
#: missing scripts) — separate from genuine CPU faults in the firmware under test.
#: Native crashes from hook bookkeeping show up here too (Renode can take an
#: AccessViolation when watchpoint hooks are manipulated after disposal).
MODEL_ERROR_RE = re.compile(
    r"(?i)(microsoft\.scripting|unhandled exception\.|ironpython|"
    r"accessviolation|no attribute)"
)
FAULT_RE = re.compile(r"(?i)\b(hardfault|hard fault|usagefault|busfault|memmanage|"
                      r"prefetch abort|data abort|undefined instruction|aborted)\b")
FATAL_RE = re.compile(r"(?i)\b(fatal|critical|fatalerror|exception|could not|failed to)\b")
#: An existing model that does not implement a register the firmware touched:
#: this is the "peripheral fidelity" signal that drives the next implementation.
MODEL_OFFSET_RE = re.compile(
    r"(?i)\[(?:WARNING|ERROR)\]\s*([A-Za-z0-9_]+):\s*Unhandled\s+(read|write)\s+(?:from|to)\s+"
    r"offset\s+0x([0-9a-f]+)"
)
#: The firmware asking for a platform reset; a boot loop shows up as many of these.
RESET_RE = re.compile(r"(?i)nvic:\s*Resetting platform with (\w+)")
#: Every (re)start of the CPU, with the vector table it booted from.
CPU_INIT_RE = re.compile(
    r"(?i)cpu:\s*Setting initial values: PC = (0x[0-9a-f]+), SP = (0x[0-9a-f]+)"
)


@dataclass
class AccessHit:
    """One distinct unmapped address and how often it was touched."""

    address: int
    kind: str
    count: int = 1

    def describe(self) -> str:
        region = memmap.peripheral_for(self.address)
        owner = "unmapped"
        note = ""
        if region is not None:
            owner = region.name
            note = "modelled" if region.is_modelled else "stub:%s" % region.stub
        return "  0x%08X %-5s %4dx  %-10s %s" % (
            self.address,
            self.kind,
            self.count,
            owner,
            note,
        )


@dataclass
class LogReport:
    """A digest of one emulator session."""

    accesses: List[AccessHit] = field(default_factory=list)
    model_offsets: Dict[Tuple[str, int, str], int] = field(default_factory=dict)
    resets: Dict[str, int] = field(default_factory=dict)
    cpu_starts: List[Tuple[int, int]] = field(default_factory=list)
    faults: List[str] = field(default_factory=list)
    model_errors: List[str] = field(default_factory=list)
    fatal: List[str] = field(default_factory=list)
    uart_markers: List[str] = field(default_factory=list)

    @property
    def distinct_addresses(self) -> int:
        return len(self.accesses)

    @property
    def reset_count(self) -> int:
        return sum(self.resets.values())

    @property
    def boot_loops(self) -> bool:
        """More than a couple of restarts means the firmware is stuck resetting."""
        return self.reset_count > 2 or len(self.cpu_starts) > 2

    def top_model_offsets(self, limit: int = 15) -> List[str]:
        ordered = sorted(self.model_offsets.items(), key=lambda item: -item[1])[:limit]
        return [
            "    %-10s offset 0x%02X %-5s %6dx" % (name, offset, kind, count)
            for (name, offset, kind), count in ordered
        ]

    def render(self, limit: int = 25) -> str:
        lines = ["emulator log digest:"]
        lines.append("  unmapped accesses: %d distinct address(es)" % self.distinct_addresses)
        for hit in self.accesses[:limit]:
            lines.append(hit.describe())
        if self.distinct_addresses > limit:
            lines.append("  ... %d more" % (self.distinct_addresses - limit))
        if self.model_offsets:
            lines.append(
                "  registers the existing models do not implement: %d distinct" % len(self.model_offsets)
            )
            lines.extend(self.top_model_offsets())
        if self.reset_count or self.cpu_starts:
            lines.append(
                "  platform resets: %d, cpu starts: %d%s"
                % (
                    self.reset_count,
                    len(self.cpu_starts),
                    "  <-- BOOT LOOP" if self.boot_loops else "",
                )
            )
            for reason, count in sorted(self.resets.items(), key=lambda item: -item[1]):
                lines.append("    %s x%d" % (reason, count))
            if self.cpu_starts:
                pc, sp = self.cpu_starts[-1]
                lines.append("    last boot: PC=0x%08X SP=0x%08X" % (pc, sp))
        if self.faults:
            lines.append("  CPU faults: %d" % len(self.faults))
            lines.extend("    %s" % line for line in self.faults[:5])
        if self.model_errors:
            lines.append("  emulator model errors: %d" % len(self.model_errors))
            lines.extend("    %s" % line for line in self.model_errors[:5])
        if self.fatal:
            lines.append("  fatal/diagnostic messages: %d" % len(self.fatal))
            lines.extend("    %s" % line for line in self.fatal[:10])
        return "\n".join(lines)

    def missing_peripherals(self) -> List[str]:
        """Peripheral names that were touched while served only by a stub."""
        names: List[str] = []
        for hit in self.accesses:
            entry = memmap.peripheral_for(hit.address)
            if entry is None:
                label = "0x%08X (unmapped)" % hit.address
            elif entry.is_modelled:
                continue
            else:
                label = entry.name
            if label not in names:
                names.append(label)
        return names


#: Renode colourises its console output; strip it before matching.
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """Remove ANSI colour/steering codes from emulator output."""
    return ANSI_RE.sub("", text)


def _trim(line: str, width: int = 180) -> str:
    text = line.strip()
    return text if len(text) <= width else text[: width - 3] + "..."


def analyse(log_text: str) -> LogReport:
    """Parse a Renode log into a :class:`LogReport`."""
    report = LogReport()
    hits: Dict[Tuple[int, str], AccessHit] = {}

    for raw_line in strip_ansi(log_text).splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        offset_match = MODEL_OFFSET_RE.search(line)
        if offset_match:
            key = (
                offset_match.group(1),
                int(offset_match.group(3), 16),
                offset_match.group(2).lower(),
            )
            report.model_offsets[key] = report.model_offsets.get(key, 0) + 1
            continue
        reset_match = RESET_RE.search(line)
        if reset_match:
            reason = reset_match.group(1)
            report.resets[reason] = report.resets.get(reason, 0) + 1
            continue
        init_match = CPU_INIT_RE.search(line)
        if init_match:
            report.cpu_starts.append(
                (int(init_match.group(1), 16), int(init_match.group(2), 16))
            )
            continue
        match = UNHANDLED_RE.search(line)
        if match:
            kind = match.group(1).lower()
            address = int(match.group(2), 16)
            key = (address, kind)
            if key in hits:
                hits[key].count += 1
            else:
                hits[key] = AccessHit(address, kind, 1)
            continue
        range_match = UNHANDLED_RANGE_RE.search(line)
        if range_match:
            address = int(range_match.group(1), 16)
            key = (address, "range")
            if key in hits:
                hits[key].count += 1
            else:
                hits[key] = AccessHit(address, "range", 1)
            continue
        if MODEL_ERROR_RE.search(line):
            report.model_errors.append(_trim(line))
            continue
        if FAULT_RE.search(line):
            report.faults.append(_trim(line))
            continue
        if FATAL_RE.search(line) and "0x" in line:
            report.fatal.append(_trim(line))
            continue
        if "FLASH_DUMP" in line or "Startup complete" in line:
            report.uart_markers.append(_trim(line))

    report.accesses = sorted(hits.values(), key=lambda hit: -hit.count)
    return report
