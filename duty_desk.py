"""当班收件：把监测站、机箱无线页和巡河车调度并成可以出示的采样证据。

模块只回答一个问题：**此刻能证明哪个断面的哪个水文阶段有可送检的实物样**。
证据分三层，缺一层都不算有样：

* 时刻——校时单给出触发的可能真实时段，只有整个时段落在同一水文阶段窗内才算确定；
* 实物——步进必须紧邻一条位置记录；卡位、重启、缺段和瓶位号重用都不能算进瓶；
* 送检——瓶要在箱的空白样覆盖内，计划送达不晚于冷藏截止，退回则当前失效。

页面的晚到、重复和修订只改变证据，不新增一次取样。已经送检的登记在首次看到时
连同当时的页、校时单和空白样存证，之后的修订不改写它当时交的凭据。

命令行走 ``duty_cli.py``。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DAY = 86400
LAST_CALL = 240        # 车次预约角前的这段时间里必须占位（秒）


# ------------------------------------------------------------------ 时间与区间


def seconds(value: str) -> int:
    """把 ``HH:MM:SS`` 折成当日秒数。"""
    try:
        hour, minute, second = (int(part) for part in value.split(":"))
    except (AttributeError, ValueError) as error:
        raise ValueError(f"bad clock {value!r}") from error
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        raise ValueError(f"bad clock {value!r}")
    return hour * 3600 + minute * 60 + second


def show(value: int) -> str:
    value %= DAY
    return f"{value // 3600:02d}:{value % 3600 // 60:02d}:{value % 60:02d}"


@dataclass(frozen=True, order=True)
class Span:
    """闭区间，单位秒。校时后的可能时刻与现场实际时刻都用它。"""

    lo: int
    hi: int

    def __post_init__(self) -> None:
        if self.hi < self.lo:
            raise ValueError("span ends before it starts")

    @classmethod
    def at(cls, value: int) -> "Span":
        return cls(value, value)

    @classmethod
    def of(cls, pair: Sequence[Any]) -> "Span":
        return cls(seconds(str(pair[0])), seconds(str(pair[1])))

    def contained(self, window: tuple[int, int]) -> bool:
        """整段落在左闭右开的阶段窗内；可能区间本身是闭的。"""
        return window[0] <= self.lo and self.hi < window[1]

    def meets(self, window: tuple[int, int]) -> bool:
        return self.lo < window[1] and window[0] <= self.hi

    def as_list(self) -> list[str]:
        return [show(self.lo), show(self.hi)]

    def __str__(self) -> str:
        return f"{show(self.lo)}..{show(self.hi)}"


def window_of(pair: Sequence[Any]) -> tuple[int, int]:
    return seconds(str(pair[0])), seconds(str(pair[1]))


def canonical(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# ---------------------------------------------------------------------- 取数


class TransportError(RuntimeError):
    """演练服务整体不可达；与单个接口的业务性拒绝区分开。"""


class Source:
    """演练服务的最小客户端。业务状态码交给调用者，只有网络故障才抛错。"""

    def __init__(self, base: str, timeout: float = 2.0, opener=urlopen):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.opener = opener

    def _open(self, request: Request):
        try:
            try:
                return self.opener(request, timeout=self.timeout)
            except HTTPError as error:
                return error
        except URLError as error:
            raise TransportError(str(error)) from error

    def request(self, path: str, params: Mapping[str, str] | None = None,
                payload: Any = None) -> tuple[int, dict[str, str], bytes]:
        url = self.base + path
        if params:
            url += "?" + urlencode(dict(params))
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        with self._open(Request(url, data=data, headers=headers)) as response:
            status = getattr(response, "status", None) or response.code
            return status, {key.lower(): value for key, value in response.headers.items()}, response.read()

    def json(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        status, _, body = self.request(path, params)
        if status != 200:
            raise TransportError(f"{path}: HTTP {status}")
        return json.loads(body)

    def now(self) -> str:
        return str(self.json("/")["time"])

    def set_clock(self, moment: str) -> str:
        status, _, body = self.request("/clock", payload={"time": moment})
        if status != 200:
            raise TransportError(f"/clock: HTTP {status} {body[:120]!r}")
        return str(json.loads(body)["time"])


# ---------------------------------------------------------------------- 资料


@dataclass(frozen=True)
class Trigger:
    station: str
    cab: str
    epoch: str
    id: str
    seen: str
    local: str

    @property
    def machine(self) -> tuple[str, str]:
        return self.cab, self.epoch

    @property
    def key(self) -> tuple[str, str, str, str]:
        return self.station, self.cab, self.epoch, self.id

    @property
    def tag(self) -> str:
        return f"{self.station}/{self.cab}/{self.epoch}/{self.id}"


@dataclass
class Page:
    id: str
    station: str
    cab: str
    epoch: str
    channel: str
    listed: str
    ready: str
    expires: str
    first: int
    last: int
    replaces: str = ""
    events: list[dict[str, Any]] | None = None
    granted_slot: str | None = None
    digest: str | None = None
    fetched_at: str | None = None
    dead: str | None = None

    @property
    def machine(self) -> tuple[str, str]:
        return self.cab, self.epoch

    def covers(self, seq: int) -> bool:
        return self.first <= seq <= self.last

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Page":
        first, last = row["range"]
        return cls(row["id"], row["station"], row["cab"], row["epoch"], row["channel"],
                   row["listed"], row["ready"], row["expires"], int(first), int(last),
                   row.get("replaces") or "")


@dataclass(frozen=True)
class Gap:
    station: str
    cab: str
    epoch: str
    first: int
    last: int
    known: str
    state: str          # wait_relay / no_records / offline

    @property
    def machine(self) -> tuple[str, str]:
        return self.cab, self.epoch

    def covers(self, seq: int) -> bool:
        return self.first <= seq <= self.last


@dataclass(frozen=True)
class ClockRule:
    id: str
    cab: str
    epoch: str
    known: str
    local: tuple[int, int]
    offset: tuple[int, int]
    replaces: str = ""

    @property
    def machine(self) -> tuple[str, str]:
        return self.cab, self.epoch

    def applies(self, local: str) -> bool:
        point = seconds(local)
        return self.local[0] <= point < self.local[1]

    def possible(self, local: str) -> Span:
        point = seconds(local)
        return Span(point + self.offset[0], point + self.offset[1])

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ClockRule":
        offset = row["offset_seconds"]
        return cls(row["id"], row["cab"], row["epoch"], row["known"],
                   window_of(row["local"]), (int(offset[0]), int(offset[1])),
                   row.get("replaces") or "")


@dataclass
class BoxVersion:
    box: str
    station: str
    cab: str
    epoch: str
    known: str
    correction: str = ""
    slots: dict[str, str] = field(default_factory=dict)
    blank_id: str | None = None
    blank_accepted: bool = False
    covers: tuple[str, ...] = ()
    cold_by: str | None = None
    expected: str | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return self.box, self.station, self.cab, self.epoch

    def copy(self) -> "BoxVersion":
        return BoxVersion(self.box, self.station, self.cab, self.epoch, self.known, self.correction,
                          dict(self.slots), self.blank_id, self.blank_accepted, tuple(self.covers),
                          self.cold_by, self.expected)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "BoxVersion":
        blank = row.get("blank") or {}
        return cls(
            row["box"], row["station"], row["cab"], row["epoch"], row["known"],
            row.get("correction", ""),
            {str(key): value for key, value in (row.get("slots") or {}).items()},
            blank.get("id"),
            bool(blank.get("accepted")),
            tuple(blank.get("covers") or ()),
            row.get("cold_by"),
            row.get("expected_delivery"),
        )


@dataclass(frozen=True)
class WeatherVersion:
    id: str
    station: str
    event: str
    known: str
    rev: int
    windows: Mapping[str, tuple[int, int]]
    replaces: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "WeatherVersion":
        return cls(row["id"], row["station"], row["event"], row["known"], int(row["rev"]),
                   {phase: window_of(span) for phase, span in row["windows"].items()},
                   row.get("replaces") or "")


@dataclass(frozen=True)
class LabEntry:
    station: str
    box: str
    bottle: str
    state: str
    known: str
    reason: str | None = None
    receipt: str | None = None
    arrival: str | None = None
    basis: tuple[str, ...] = ()
    trip: str | None = None
    at: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)
    order: int = 0

    @property
    def key(self) -> tuple[str, str, str, str]:
        return self.station, self.box, self.bottle, self.known


@dataclass(frozen=True)
class Trip:
    trip: str
    book_by: str
    stations: Mapping[str, str]
    box_slots: tuple[str, ...]
    first_visible: str | None = None


@dataclass
class Booking:
    request: str
    window: str
    trip: str
    station: str
    bottle_set: str
    event: str
    phase: str
    created: str
    state: str = "in_flight"          # in_flight / accepted / rejected / unreachable
    receipt: Mapping[str, Any] | None = None
    note: str = ""

    def travel(self) -> dict[str, Any]:
        return {"request": self.request, "trip": self.trip, "station": self.station,
                "event": self.event, "phase": self.phase, "bottle_set": self.bottle_set}

    @property
    def live(self) -> bool:
        return self.state in ("in_flight", "accepted")


# ------------------------------------------------------------------ 证据结论


@dataclass
class BottleResult:
    state: str                     # proven / pending / lost
    code: str
    reason: str
    slot: str | None = None
    label: str | None = None
    box: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "code": self.code, "reason": self.reason,
                "slot": self.slot, "label": self.label, "box": self.box}


@dataclass
class PhaseResult:
    state: str                     # proven / straddle / outside / unknown
    phase: str | None
    reason: str
    adjacent: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "phase": self.phase, "reason": self.reason,
                "adjacent": list(self.adjacent)}


@dataclass
class Delivery:
    state: str                     # ok / cold_passed / incomplete / not_covered
    reason: str
    cold_by: str | None = None
    expected: str | None = None
    blank: str | None = None
    covers: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "reason": self.reason, "cold_by": self.cold_by,
                "expected_delivery": self.expected, "blank": self.blank,
                "covers": list(self.covers)}


@dataclass
class Sample:
    """一个触发或一条现场记录能证明到什么程度。"""

    state: str                        # proven / pending / lost
    reason: str
    origin: str                       # trigger / field
    ref: str
    basis: tuple[tuple[str, str, str], ...] = ()     # (kind, id, digest)
    possible: Span | None = None
    bottle: tuple[str, str, str] | None = None       # box, label, cab/epoch
    cold_by: str | None = None
    lab: str | None = None
    lab_receipt: str | None = None
    phases: Mapping[str, str] = field(default_factory=dict)   # event -> 阶段

    @property
    def checkable(self) -> bool:
        return self.state == "proven"

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "origin": self.origin,
            "ref": self.ref,
            "basis": [{"kind": kind, "id": ident, "digest": dig} for kind, ident, dig in self.basis],
            "possible": self.possible.as_list() if self.possible else None,
            "phases": dict(self.phases),
            "bottle": list(self.bottle) if self.bottle else None,
            "cold_by": self.cold_by,
            "lab": self.lab,
            "lab_receipt": self.lab_receipt,
        }


# ---------------------------------------------------------------------- 推理


class Evidence:
    """一份资料快照上的纯推理。不联网、不记状态，方便随时重算。"""

    def __init__(self, now: str, triggers: Iterable[Trigger], pages: Iterable[Page],
                 gaps: Iterable[Gap], clocks: Iterable[ClockRule], boxes: Iterable[BoxVersion],
                 weather: Iterable[WeatherVersion], lab: Iterable[LabEntry]):
        self.now = seconds(now)
        self.now_text = now
        self.triggers = list(triggers)
        self.pages = list(pages)
        self.gaps = list(gaps)
        self.clocks = list(clocks)
        self.boxes = list(boxes)
        self.weather = list(weather)
        self.lab = list(lab)
        self._live: dict[tuple[str, str], list[Page]] = {}
        self._boxes: dict[tuple[str, str, str], list[BoxVersion]] = {}

    # -- 校时 -------------------------------------------------------------

    def clock_rule(self, trigger: Trigger) -> ClockRule | None:
        rules = [rule for rule in self.clocks if rule.machine == trigger.machine]
        if not rules:
            return None
        replaced = {rule.replaces for rule in rules if rule.replaces}
        live = [rule for rule in rules if rule.id not in replaced]
        covering = [rule for rule in live if rule.applies(trigger.local)]
        if not covering:
            covering = [rule for rule in live if rule.known <= self.now_text]
        if not covering:
            covering = [rule for rule in rules if rule.applies(trigger.local)]
        if not covering:
            return None
        return max(covering, key=lambda rule: (rule.known, rule.id))

    def possible(self, trigger: Trigger) -> tuple[Span | None, ClockRule | None]:
        rule = self.clock_rule(trigger)
        if rule is None:
            return None, None
        return rule.possible(trigger.local), rule

    # -- 水文 -------------------------------------------------------------

    def weather_for(self, station: str, event: str) -> WeatherVersion | None:
        rows = [row for row in self.weather if row.station == station and row.event == event]
        if not rows:
            return None
        replaced = {row.replaces for row in rows if row.replaces}
        live = [row for row in rows if row.id not in replaced] or rows
        return max(live, key=lambda row: (row.rev, row.known, row.id))

    def phase_at(self, station: str, event: str, span: Span) -> PhaseResult:
        version = self.weather_for(station, event)
        if version is None:
            return PhaseResult("unknown", None, "没有该断面的水文单")
        inside = [phase for phase, window in version.windows.items() if span.contained(window)]
        if len(inside) == 1:
            return PhaseResult("proven", inside[0], "可能时段整个落在该阶段窗内")
        if len(inside) > 1:
            return PhaseResult("straddle", None, "可能时段同时落在多个阶段窗内", tuple(inside))
        touching = sorted((phase for phase, window in version.windows.items() if span.meets(window)),
                          key=lambda phase: version.windows[phase][0])
        if len(touching) >= 2:
            return PhaseResult("straddle", None, "可能时段跨在阶段窗之间，不能确定", tuple(touching))
        return PhaseResult("outside", None, "可能时段不在任何阶段窗内", tuple(touching))

    # -- 机箱页与缺段 ------------------------------------------------------

    def live_pages(self, machine: tuple[str, str]) -> list[Page]:
        if machine in self._live:
            return self._live[machine]
        declared = [page for page in self.pages if page.machine == machine]
        superseded = {page.replaces for page in declared if page.replaces}
        # replaces 可以传递：A 被 B 替换、B 又被 C 替换时，A 也不再是当前版本。
        # 只有"替换了别人、自己也被替换"的版本才继续向下传，替换者本身留作当前版。
        changed = True
        while changed:
            changed = False
            for page in declared:
                if page.id in superseded and page.replaces and page.replaces not in superseded:
                    superseded.add(page.replaces)
                    changed = True
        live = [page for page in declared if page.id not in superseded]
        self._live[machine] = live
        return live

    def usable_pages(self, machine: tuple[str, str]) -> list[Page]:
        """当前版页。

        修订页一经清单发布就替换旧版：旧版立即退出，即使新版还没到——否则会拿
        柜端已撤回的页去证明一个后来被改掉的瓶位。新版未到期间该段统一按待证。
        """
        return self.live_pages(machine)

    def _record(self, machine: tuple[str, str], seq: int) -> tuple[dict[str, Any] | None, str, str]:
        """返回 (记录, 代码, 附注)。代码 record / gap:… / page:… / missing / conflict。"""
        hits: list[tuple[dict[str, Any], Page]] = []
        for page in self.usable_pages(machine):
            if page.covers(seq) and page.events is not None:
                event = next((item for item in page.events if item.get("seq") == seq), None)
                if event is not None:
                    hits.append((event, page))
        if hits:
            bodies = {canonical(event) for event, _ in hits}
            if len(bodies) > 1:
                return None, "conflict", "同号在不同页里内容不一致"
            return hits[0][0], "record", hits[0][1].id
        gap = next((item for item in self.gaps if item.machine == machine and item.covers(seq)), None)
        if gap is not None:
            return None, "gap:" + gap.state, gap.state
        waiting = [page for page in self.live_pages(machine)
                   if page.covers(seq) and page.events is None and page.dead is None]
        if waiting:
            page = sorted(waiting, key=lambda item: (item.expires, item.id))[0]
            return None, "page:" + page.id, page.id
        return None, "missing", ""

    def bottle(self, trigger: Trigger, seq: int) -> BottleResult:
        event, code, note = self._record(trigger.machine, seq)
        if code == "conflict":
            return BottleResult("pending", "conflict", note)
        if code == "gap:no_records":
            return BottleResult("lost", "gap_no_records", "柜端确认该段没有记录，不能证明进入实物瓶")
        if code == "gap:offline":
            return BottleResult("pending", "gap_offline", "柜端当前离线，该段待回，暂不能证明")
        if code == "gap:wait_relay":
            return BottleResult("pending", "gap_relay", "该段待另一无线传送，可从补领窗口追回")
        if code.startswith("page:"):
            return BottleResult("pending", "pending_page",
                                f"覆盖 {seq} 号的页 {note} 尚未领到")
        if code == "missing":
            return BottleResult("pending", "missing", f"{seq} 号既无页也无缺段说明")

        kind = event.get("kind")
        if kind != "pulse":
            return BottleResult("pending", "not_pulse", f"{seq} 号不是触发脉冲")
        step = int(event.get("step", -1))
        if step != seq - 1:
            return BottleResult("lost", "non_adjacent", "脉冲引用的不是紧邻的前一记录，换瓶不能视为连续")
        before, before_code, before_note = self._record(trigger.machine, step)
        if before_code == "gap:no_records":
            return BottleResult("lost", "gap_no_records", "脉冲前的位置记录确认不存在")
        if before_code == "gap:offline":
            return BottleResult("pending", "gap_offline", "脉冲前的位置记录离线待回")
        if before_code == "gap:wait_relay":
            return BottleResult("pending", "gap_relay", "脉冲前的位置记录待另一无线传送")
        if before_code.startswith("page:"):
            return BottleResult("pending", "pending_page",
                                f"脉冲前的位置记录在尚未领到的页 {before_note} 里")
        if before_code == "conflict":
            return BottleResult("pending", "conflict", before_note)
        if before_code != "record":
            return BottleResult("pending", "missing", "脉冲前没有可用的位置记录")
        assert before is not None
        if before.get("kind") == "valve":
            return BottleResult("pending", "valve_jam",
                                f"前一记录 {step} 是卡位，不能证明进入实物瓶")
        if before.get("kind") == "restart":
            return BottleResult("pending", "restart",
                                f"前一记录 {step} 只是重启，只界定新世代，不能证明换瓶")
        if before.get("kind") != "position":
            return BottleResult("pending", "not_position",
                                f"前一记录 {step} 不是位置记录（{before.get('kind')}）")
        slot = before.get("slot")
        if slot is None:
            return BottleResult("pending", "no_slot", "位置记录没有可对照的瓶位")

        box, label = self._map_slot(trigger, str(slot))
        if box is None or label is None:
            return BottleResult("pending", "no_mapping",
                                f"瓶位 {slot} 在该柜/世代的箱资料里找不到对应空间号")
        return BottleResult("proven", "proven",
                            f"脉冲 {seq} 紧接位置 {step}，对应 {box}/{label}",
                            slot=str(slot), box=box, label=label)

    # -- 冷箱与送达 --------------------------------------------------------

    def merged_boxes(self, station: str, cab: str, epoch: str) -> list[BoxVersion]:
        key = (station, cab, epoch)
        if key in self._boxes:
            return [box.copy() for box in self._boxes[key]]
        versions = sorted((row for row in self.boxes
                           if (row.station, row.cab, row.epoch) == key),
                          key=lambda row: (row.known, row.correction or ""))
        merged: dict[str, BoxVersion] = {}
        for row in versions:
            current = merged.get(row.box)
            if current is None:
                merged[row.box] = row.copy()
                continue
            # 新资料只替换同一箱上公告的字段。
            if row.slots:
                current.slots.update(row.slots)
            if row.blank_id is not None:
                current.blank_id = row.blank_id
                current.blank_accepted = row.blank_accepted
                current.covers = tuple(row.covers)
            if row.cold_by is not None:
                current.cold_by = row.cold_by
            if row.expected is not None:
                current.expected = row.expected
            current.known = max(current.known, row.known)
        result = list(merged.values())
        self._boxes[key] = [box.copy() for box in result]
        return result

    def _map_slot(self, trigger: Trigger, slot: str) -> tuple[str | None, str | None]:
        for box in self.merged_boxes(trigger.station, trigger.cab, trigger.epoch):
            label = box.slots.get(slot)
            if label is not None:
                return box.box, label
        return None, None

    def delivery(self, station: str, cab: str, epoch: str, box_name: str, label: str,
                 moment: int | None = None) -> Delivery:
        box = next((item for item in self.merged_boxes(station, cab, epoch)
                    if item.box == box_name), None)
        if box is None:
            return Delivery("incomplete", "没有该箱的实物资料")
        if not box.blank_accepted:
            return Delivery("incomplete", "箱的空白样未接受", blank=box.blank_id,
                            cold_by=box.cold_by, expected=box.expected, covers=box.covers)
        if label not in box.covers:
            return Delivery("not_covered", f"空白样 {box.blank_id} 的覆盖范围不含 {label}",
                            blank=box.blank_id, cold_by=box.cold_by,
                            expected=box.expected, covers=box.covers)
        if box.cold_by is None or box.expected is None:
            return Delivery("incomplete", "箱资料缺冷藏截止或计划送达", blank=box.blank_id,
                            cold_by=box.cold_by, expected=box.expected, covers=box.covers)
        if seconds(box.expected) > seconds(box.cold_by):
            return Delivery("cold_passed", "计划送达晚于冷藏截止", blank=box.blank_id,
                            cold_by=box.cold_by, expected=box.expected, covers=box.covers)
        now = self.now if moment is None else moment
        if now > seconds(box.cold_by):
            return Delivery("cold_passed", "已过冷藏截止", blank=box.blank_id,
                            cold_by=box.cold_by, expected=box.expected, covers=box.covers)
        return Delivery("ok", "空白样覆盖且计划送达不晚于冷藏截止", blank=box.blank_id,
                        cold_by=box.cold_by, expected=box.expected, covers=box.covers)

    # -- 送检回条 ----------------------------------------------------------

    def lab_for(self, station: str, box: str, label: str) -> LabEntry | None:
        rows = [row for row in self.lab
                if row.station == station and row.box == box and row.bottle == label
                and row.state != "field_sample"]
        if not rows:
            return None
        # 同一已知时刻的重复按内容定序；较新的回条（收到/退回）覆盖旧状态。
        return max(rows, key=lambda row: (row.known, row.order))


# ---------------------------------------------------------------------- 台账


@dataclass
class WindowResult:
    item: Mapping[str, Any]
    span: tuple[int, int] | None
    status: str          # proven / pending / open / unfillable
    reason: str
    sample: Sample | None = None
    reviewed_with: str | None = None

    @property
    def id(self) -> str:
        return str(self.item["id"])

    @property
    def station(self) -> str:
        return str(self.item["station"])

    @property
    def phase(self) -> str:
        return str(self.item["phase"])

    @property
    def arrive_by(self) -> str | None:
        value = self.item.get("arrive_by")
        return str(value) if value is not None else None

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "event": self.item["event"], "station": self.station,
                "phase": self.phase, "arrive_by": self.arrive_by,
                "span": [show(self.span[0]), show(self.span[1])] if self.span else None,
                "status": self.status, "reason": self.reason,
                "reviewed_with": self.reviewed_with,
                "sample": self.sample.as_dict() if self.sample else None}


@dataclass
class PairResult:
    item: Mapping[str, Any]
    status: str
    reason: str
    upstream: WindowResult | None = None
    downstream: WindowResult | None = None
    spread: Span | None = None
    travel: tuple[int, int] = (0, 0)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.item["id"], "event": self.item["event"], "status": self.status,
                "reason": self.reason,
                "upstream": self.upstream.as_dict() if self.upstream else None,
                "downstream": self.downstream.as_dict() if self.downstream else None,
                "spread": self.spread.as_list() if self.spread else None,
                "travel_seconds": [show(self.travel[0]), show(self.travel[1])]}


class Desk:
    """值班台账：保留已见资料，按当前时钟决定还能追什么、该占哪班车。"""

    def __init__(self, source: Source):
        self.source = source
        self.now = "00:00:00"
        self.server_time = "00:00:00"
        self.started = False

        self.triggers: dict[tuple[str, str, str, str], Trigger] = {}
        self.pages: dict[tuple[str, str], Page] = {}
        self.gaps: dict[tuple[str, str, int, int], Gap] = {}
        self.clocks: dict[str, ClockRule] = {}
        self.boxes: list[BoxVersion] = []
        self.weather: dict[str, WeatherVersion] = {}
        self.duty: dict[str, Mapping[str, Any]] = {}
        self.lab: dict[tuple[str, str, str, str], LabEntry] = {}
        self.trips: dict[str, Trip] = {}
        self.bookings: dict[str, Booking] = {}
        self.archives: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.notes: list[dict[str, Any]] = []
        self.slot_spend: dict[tuple[str, str], int] = {}
        self.revised: set[str] = set()
        self.window_seen: dict[str, str] = {}
        self.transitions: list[dict[str, Any]] = []

    # -- 底层 -------------------------------------------------------------

    def _note(self, kind: str, message: str) -> None:
        entry = {"at": self.now, "kind": kind, "message": message}
        if entry not in self.notes:
            self.notes.append(entry)

    def tick(self, moment: str | None = None, pull: bool = True) -> dict[str, Any]:
        """拨时钟（可省略）→ 收一轮资料 → 领该领的页 → 续在途回执 → 占车 → 出报告。

        ``pull=False`` 只重算台账，方便在实验里重放同一份快照。
        """
        if moment is not None:
            if not (isinstance(moment, str) and len(moment) == 8 and moment[2] == ":" and moment[5] == ":"):
                raise ValueError(f"bad clock {moment!r}")
            self.source.set_clock(moment)
        current = self.source.now()
        if seconds(current) < seconds(self.server_time):
            raise TransportError("服务时钟倒退，装置可能已重启")
        self.server_time = current
        self.now = current
        if not self.started:
            self.started = True
            self._note("start", f"当班开始于 {current}")
        if pull:
            self.pull()
            self.fetch_pages()
            self.confirm_bookings()
            self.book_planned()
        return self.status()

    # -- 资料 -------------------------------------------------------------

    def pull(self) -> None:
        for path, key, sink in (
            ("/weather", "entries", self._take_weather),
            ("/clocks", "entries", self._take_clocks),
            ("/boxes", "entries", self._take_boxes),
            ("/duty", "entries", self._take_duty),
            ("/lab", "entries", self._take_lab),
            ("/trips", "trips", self._take_trips),
        ):
            status, _, body = self.source.request(path)
            if status != 200:
                self._note("source", f"{path} 返回 {status}")
                continue
            for row in json.loads(body)[key]:
                sink(row)
        self._pull_station()
        self._pull_radio()
        self._archive_lab()

    def _take_weather(self, row: Mapping[str, Any]) -> None:
        version = WeatherVersion.from_row(row)
        if version.id not in self.weather:
            self.weather[version.id] = version
            if version.replaces and version.replaces not in self.revised:
                self.revised.add(version.replaces)
                self.revised.add(version.id)
                self._note("weather",
                           f"{version.id} 替换 {version.replaces}（rev {version.rev}）："
                           f"只重核 {version.station}/{version.event} 的窗与配对")

    def _take_clocks(self, row: Mapping[str, Any]) -> None:
        rule = ClockRule.from_row(row)
        self.clocks.setdefault(rule.id, rule)

    def _take_boxes(self, row: Mapping[str, Any]) -> None:
        version = BoxVersion.from_row(row)
        if all(item.key != version.key or (item.known, item.correction) != (version.known, version.correction)
               for item in self.boxes):
            self.boxes.append(version)
            if version.correction:
                self._note("boxes", f"{version.box} 更正 {version.correction}：只动同箱公告字段")

    def _take_duty(self, row: Mapping[str, Any]) -> None:
        self.duty[str(row["id"])] = row

    def _take_lab(self, row: Mapping[str, Any]) -> None:
        entry = LabEntry(row["station"], row["box"], row["bottle"], row["state"], row["known"],
                         row.get("reason"), row.get("receipt"), row.get("arrival"),
                         tuple(row.get("basis") or ()), row.get("trip"), row.get("at"),
                         dict(row), len(self.lab))
        if entry.key not in self.lab:
            self.lab[entry.key] = entry
            self._note("lab", f"{entry.station} {entry.box}/{entry.bottle} 回条：{entry.state}")

    def _take_trips(self, row: Mapping[str, Any]) -> None:
        self.trips[row["trip"]] = Trip(
            row["trip"], row["book_by"], {key: value for key, value in row["stations"].items()},
            tuple(row["box_slots"]), row.get("first_visible"))

    def stations(self) -> list[str]:
        found = {str(row["station"]) for row in self.duty.values() if row.get("station")}
        found |= {trigger.station for trigger in self.triggers.values()}
        return sorted(found)

    def _pull_station(self) -> None:
        for station in self.stations():
            status, _, body = self.source.request("/station", {"station": station})
            if status != 200:
                self._note("station", f"{station} 摘要返回 {status}")
                continue
            for row in json.loads(body)["events"]:
                trigger = Trigger(row["station"], row["cab"], row["epoch"], row["id"],
                                  row["seen"], row["local_time"])
                self.triggers.setdefault(trigger.key, trigger)

    def _pull_radio(self) -> None:
        for cab in sorted({trigger.cab for trigger in self.triggers.values()}):
            status, _, body = self.source.request("/radio", {"cab": cab})
            if status != 200:
                self._note("radio", f"{cab} 清单返回 {status}")
                continue
            payload = json.loads(body)
            for row in payload["pages"]:
                page = Page.from_row(row)
                old = self.pages.get((page.cab, page.id))
                if old is None:
                    self.pages[(page.cab, page.id)] = page
                    self._note("page", f"清单新增 {page.id}（{page.channel}，段 {page.first}-{page.last}"
                                       + (f"，替换 {page.replaces}" if page.replaces else "") + "）")
                else:
                    old.ready, old.expires = page.ready, page.expires
                    old.first, old.last = page.first, page.last
                    old.replaces = page.replaces
                    old.channel, old.listed = page.channel, page.listed
            for row in payload["gaps"]:
                gap = Gap(row["station"], row["cab"], row["epoch"], int(row["range"][0]),
                          int(row["range"][1]), row["known"], row["state"])
                self.gaps.setdefault((gap.cab, gap.epoch, gap.first, gap.last), gap)

    # -- 领页 -------------------------------------------------------------

    def _slot_now(self, channel: str) -> Mapping[str, Any] | None:
        for slot in self.source.json("/radio/slots")["slots"]:
            if slot["channel"] == channel and seconds(str(slot["from"])) <= seconds(self.now) \
                    < seconds(str(slot["until"])):
                return slot
        return None

    def _spent(self, slot: Mapping[str, Any]) -> int:
        return self.slot_spend.get((str(slot["slot"]), str(slot["from"])), 0)

    def _wanted(self) -> list[tuple[Page, int]]:
        """还没领到的当前版页，按"能不能决定出车"打分。"""
        history = self.history()
        scored: list[tuple[Page, int]] = []
        for page in self.pages.values():
            if page.dead is not None or page.events is not None:
                continue
            if page not in history.live_pages(page.machine):
                continue
            if page.expires <= self.now:
                continue
            score = 1
            for gap in self.gaps.values():
                if gap.machine == page.machine and gap.state == "wait_relay" \
                        and page.covers(gap.first):
                    score += 2
            for trigger in self.triggers.values():
                if trigger.machine != page.machine:
                    continue
                possible, _ = history.possible(trigger)
                if possible is None:
                    score += 1
                    continue
                for item in self._phase_items():
                    if item.get("station") != trigger.station:
                        continue
                    span = self.window_span(trigger.station, str(item["event"]), str(item["phase"]))
                    if span and possible.meets(span):
                        soon = seconds(str(item["arrive_by"])) - seconds(self.now) <= 900
                        score = max(score, 4 if soon else 3)
            scored.append((page, score))
        scored.sort(key=lambda pair: (-pair[1], pair[0].expires, pair[0].first, pair[0].id))
        return scored

    def fetch_pages(self) -> list[dict[str, Any]]:
        """按缺段补领。通道班内页数有限，先保可能决定出车的页。"""
        log: list[dict[str, Any]] = []
        for page, score in self._wanted():
            slot = self._slot_now(page.channel)
            if slot is None:
                continue
            if seconds(self.now) < seconds(page.ready):
                continue
            if self._spent(slot) >= int(slot["pages"]):
                self._note("radio", f"{slot['slot']} 班已用满，{page.id} 只能等下一班")
                continue
            status, headers, body = self.source.request(
                "/download", {"slot": str(slot["slot"]), "page": page.id})
            self.slot_spend[(str(slot["slot"]), str(slot["from"]))] = self._spent(slot) + 1
            outcome = {"at": self.now, "page": page.id, "slot": slot["slot"], "score": score,
                       "http": status}
            if status == 200:
                claim = headers.get("x-content-sha256", "")
                actual = hashlib.sha256(body).hexdigest()
                payload = json.loads(body)
                if claim and claim != actual:
                    page.dead = "digest"
                    outcome["outcome"] = "digest_mismatch"
                    self._note("radio", f"{page.id} 摘要不符，不采信")
                elif payload.get("epoch") != page.epoch \
                        or list(payload.get("range", [])) != [page.first, page.last]:
                    page.dead = "mismatch"
                    outcome["outcome"] = "range_mismatch"
                    self._note("radio", f"{page.id} 内容与清单档位不符，不采信")
                else:
                    page.events = payload["events"]
                    page.granted_slot = str(slot["slot"])
                    page.digest = actual
                    page.fetched_at = self.now
                    outcome["outcome"] = "granted"
                    self._note("radio", f"领到 {page.id}（{slot['slot']}，段 {page.first}-{page.last}）")
            elif status == 425:
                outcome["outcome"] = "closing"
            elif status == 410:
                page.dead = "evicted"
                outcome["outcome"] = "evicted"
            elif status == 429:
                outcome["outcome"] = "quota"
            elif status == 503:
                outcome["outcome"] = "no_radio_slot"
            elif status == 404:
                page.dead = "not_declared"
                outcome["outcome"] = "not_declared"
            else:
                outcome["outcome"] = f"http_{status}"
            log.append(outcome)
        return log

    # -- 送检存证 ----------------------------------------------------------

    def _archive_lab(self) -> None:
        """首次看到送检单时按当时资料存证；后来的修订不改写当时凭据。

        每个 ``known`` 时刻的回条各自存一份（收到与退回是两件事）。回条没写 basis
        时，就从当时实际据以判定的那版页、校时单和空白样补全。
        """
        history = self.history()
        for entry in sorted(self.lab.values(), key=lambda row: (row.known, row.order)):
            if entry.key in self.archives or entry.known > self.now:
                continue
            if entry.state == "field_sample":
                continue
            versions: list[dict[str, Any]] = []
            for name in entry.basis:
                versions += self._version_of(name, entry.station, history)
            trigger = self._bottle_trigger(entry, history)
            record: dict[str, Any] = {
                "station": entry.station, "box": entry.box, "bottle": entry.bottle,
                "state": entry.state, "known": entry.known, "receipt": entry.receipt,
                "declared_basis": list(entry.basis), "versions": versions,
                "trigger_ref": None, "possible": None, "clock": None, "physical": None,
            }
            if trigger is not None:
                sample = self._sample_for(trigger, history)
                if sample is not None:
                    record["trigger_ref"] = sample.ref
                    record["possible"] = sample.possible.as_list() if sample.possible else None
                    record["clock"] = next((ident for kind, ident, _ in sample.basis
                                            if kind == "clock"), None)
                    record["physical"] = list(sample.bottle) if sample.bottle else None
                    # 回条没写 basis 时，用当时实际据以判定的那一版页/校时/空白样补全凭据。
                    have = {(item["kind"], item["id"]) for item in versions}
                    for kind, ident, dig in sample.basis:
                        if (kind, ident) in have:
                            continue
                        found = self._version_of(ident, entry.station, history)
                        if not found:
                            found = [{"kind": kind, "id": ident}]
                        for item in found:
                            versions.append({**item, "digest": dig})
                            have.add((item["kind"], item["id"]))
            record["digest"] = canonical(record)
            self.archives[entry.key] = record
            self._note("archive", f"{entry.station} {entry.box}/{entry.bottle} 按当时资料存证")

    def _version_of(self, name: str, station: str, history: Evidence) -> list[dict[str, Any]]:
        """把一个 basis 标识翻成存证用的版本快照（页 / 校时单 / 空白样）。"""
        found: list[dict[str, Any]] = []
        for page in self.pages.values():
            if page.id == name:
                found.append({"kind": "page", "id": page.id, "slot": page.granted_slot,
                              "digest": page.digest, "range": [page.first, page.last],
                              "epoch": page.epoch})
        for rule in self.clocks.values():
            if rule.id == name:
                found.append({"kind": "clock", "id": rule.id, "offset": list(rule.offset),
                              "local": [show(point) for point in rule.local]})
        blanks = [box for box in self.boxes if box.blank_id == name]
        if blanks:
            newest = max(blanks, key=lambda box: box.known)   # 当时最新的一版
            found.append({"kind": "blank", "id": name, "covers": list(newest.covers),
                          "accepted": newest.blank_accepted, "box": newest.box})
        return found

    def _bottle_trigger(self, entry: LabEntry, history: Evidence) -> Trigger | None:
        for trigger in self.triggers.values():
            if trigger.station != entry.station or history.possible(trigger)[0] is None:
                continue
            for seq in self._pulse_seqs(trigger):
                bottle = history.bottle(trigger, seq)
                if bottle.state == "proven" and bottle.box == entry.box \
                        and bottle.label == entry.bottle:
                    return trigger
        return None

    # -- 证据 -------------------------------------------------------------

    def history(self) -> Evidence:
        return Evidence(self.now, self.triggers.values(), self.pages.values(),
                        self.gaps.values(), self.clocks.values(), self.boxes,
                        self.weather.values(), self.lab.values())

    def _pulse_seqs(self, trigger: Trigger) -> list[int]:
        found: set[int] = set()
        for page in self.history().usable_pages(trigger.machine):
            for event in page.events or []:
                if event.get("kind") == "pulse" and event.get("id") == trigger.id:
                    found.add(int(event["seq"]))
        return sorted(found)

    def _sample_for(self, trigger: Trigger, history: Evidence) -> Sample | None:
        possible, rule = history.possible(trigger)
        clock_basis = (("clock", rule.id, canonical([list(rule.offset), list(rule.local)])),) if rule else ()
        seqs = self._pulse_seqs(trigger) or self._unclaimed_pulse(trigger, history)
        if not seqs:
            return Sample("pending", self._no_seq_reason(trigger, history),
                          "trigger", trigger.tag, clock_basis, possible)
        seq = seqs[0]
        ref = f"{trigger.tag}#{seq}"
        if possible is None:
            return Sample("pending", "没有覆盖该触发机内时刻的校时单", "trigger", ref, clock_basis)
        bottle = history.bottle(trigger, seq)
        if bottle.state != "proven":
            return Sample("pending" if bottle.state == "pending" else "lost",
                          bottle.reason, "trigger", ref, clock_basis, possible)
        delivery = history.delivery(trigger.station, trigger.cab, trigger.epoch,
                                    bottle.box or "", bottle.label or "")
        basis = clock_basis + tuple(
            ("page", page.id, page.digest or "") for page in history.usable_pages(trigger.machine)
            if page.covers(seq) and page.events is not None)
        basis += (("blank", delivery.blank or "", canonical(list(delivery.covers))),)
        sample = Sample("pending", "", "trigger", ref, basis, possible,
                        (bottle.box or "", bottle.label or "", f"{trigger.cab}/{trigger.epoch}"),
                        delivery.cold_by)
        lab = history.lab_for(trigger.station, bottle.box or "", bottle.label or "")
        if lab is not None:
            sample.lab = lab.state
            sample.lab_receipt = lab.receipt
        if lab is not None and lab.state == "rejected":
            sample.state, sample.reason = "lost", f"送检被退回：{lab.reason or '未注原因'}"
        elif lab is not None and lab.state == "received":
            sample.state, sample.reason = "proven", f"化验室已收（{lab.receipt}）"
        elif delivery.state == "not_covered":
            sample.state, sample.reason = "lost", delivery.reason
        elif delivery.state in ("incomplete", "cold_passed"):
            sample.state, sample.reason = "pending", delivery.reason
        else:
            sample.state, sample.reason = "proven", "实物瓶已确认且可送检"
        return sample

    def _no_seq_reason(self, trigger: Trigger, history: Evidence) -> str:
        """为什么还定位不到这个触发：修订待领、段未说明，还是摘要太粗。"""
        waiting = [page.id for page in history.live_pages(trigger.machine)
                   if page.events is None and page.dead is None]
        if waiting:
            return f"机内页（{'、'.join(sorted(waiting))}）还没领到，该段暂不能作证"
        return "摘要只有触发序号与粗时间，机内页里没有可归属的脉冲"

    def _unclaimed_pulse(self, trigger: Trigger, history: Evidence) -> list[int]:
        """摘要报过该触发，但已领页里还没出现同 id 的脉冲：找唯一能归属的脉冲。"""
        claimed = {other.id for other in self.triggers.values()
                   if other.machine == trigger.machine}
        candidates: list[int] = []
        for page in history.usable_pages(trigger.machine):
            for event in page.events or []:
                if event.get("kind") != "pulse":
                    continue
                if event.get("id") in claimed:
                    continue
                candidates.append(int(event["seq"]))
        return [candidates[0]] if len(candidates) == 1 else []

    def station_samples(self, station: str) -> list[Sample]:
        history = self.history()
        samples = [sample for trigger in sorted(self.triggers.values(), key=lambda row: row.key)
                   if trigger.station == station
                   for sample in [self._sample_for(trigger, history)] if sample]
        samples += [sample for sample in self._field_samples(history) if sample.origin == "field"
                    and self._field_station(sample) == station]
        for sample in samples:
            if sample.possible is None:
                continue
            sample.phases = {
                event: phasing.phase or phasing.state
                for event in self.events_of(station)
                for phasing in [history.phase_at(station, event, sample.possible)]
            }
        return samples

    def _field_samples(self, history: Evidence) -> list[Sample]:
        samples = []
        for entry in sorted(self.lab.values(), key=lambda row: row.known):
            if entry.state != "field_sample" or entry.known > self.now:
                continue
            at = seconds(entry.at or entry.known)
            delivery = history.delivery(entry.station, "field", "manual", entry.box, entry.bottle, at)
            sample = Sample("pending", "现场补采待核对", "field", f"FIELD:{entry.trip}@{entry.at}",
                            (("lab", f"{entry.box}/{entry.bottle}", canonical(entry.raw)),),
                            Span.at(at), (entry.box, entry.bottle, "field/manual"), delivery.cold_by)
            if delivery.state == "ok":
                sample.state, sample.reason = "proven", "按真实时刻与箱、交接状态计入"
            else:
                sample.state = "lost" if delivery.state == "not_covered" else "pending"
                sample.reason = delivery.reason
            samples.append(sample)
        return samples

    def _field_station(self, sample: Sample) -> str:
        trip = sample.ref[len("FIELD:"):].split("@")[0]
        return next((row.station for row in self.lab.values()
                     if row.state == "field_sample" and row.trip == trip), "")

    def events_of(self, station: str) -> list[str]:
        """该断面出现在工单或配对里的全部事件。"""
        found = {str(row["event"]) for row in self.duty.values()
                 if row.get("station") == station}
        for row in self.duty.values():
            if "upstream" not in row:
                continue
            sides = {str(row["upstream"][0]), str(row["downstream"][0])}
            if station in sides:
                found.add(str(row["event"]))
        return sorted(found)

    def window_span(self, station: str, event: str, phase: str) -> tuple[int, int] | None:
        version = self.history().weather_for(station, event)
        return version.windows.get(phase) if version else None

    def _candidates(self, station: str, event: str, phase: str) -> list[tuple[Sample, PhaseResult]]:
        """落在该阶段窗内的采样，或可能时段仍跨在该窗上的采样。"""
        history = self.history()
        sid = f"{station}/{phase}"
        found = []
        for sample in self.station_samples(station):
            if sample.possible is None:
                continue
            phasing = history.phase_at(station, event, sample.possible)
            if phasing.state == "proven":
                if phasing.phase == phase:
                    found.append((sample, phasing))
                continue
            if sid in phasing.adjacent:
                found.append((sample, phasing))
        return found

    def _pick(self, station: str, event: str, phase: str) -> tuple[str, Sample | None, str]:
        """返回 (已证 / 待证 / 已失 / 空, 采样, 原因)。

        同一窗里有多个候选时按证据强度排：

        1. 实物可检、可能时段整段落在窗内——已证；
        2. 实物可检但可能时段跨在阶段窗之间——待证（曾显示有样的窗要降级）；
        3. 实物待证——待证；
        4. 实物已失——窗还开就是可另采的 open，窗关了才是 unfillable。

        现场记录按真实时刻、箱与交接状态计，优先于靠推断归属的触发。
        """
        found = self._candidates(station, event, phase)
        ordered = sorted(found, key=lambda pair: (pair[0].origin != "field",
                                                  pair[0].state != "proven"))
        for sample, phasing in ordered:
            if sample.state == "proven" and phasing.state == "proven":
                return "proven", sample, sample.reason
        for sample, phasing in ordered:
            if sample.state == "proven":
                return "pending", sample, phasing.reason
        for sample, phasing in ordered:
            if sample.state == "pending":
                return "pending", sample, sample.reason
        for sample, phasing in ordered:
            if sample.state == "lost":
                return "lost", sample, sample.reason
        return "none", None, "窗内还没有可归属的采样"

    def _phase_items(self) -> list[Mapping[str, Any]]:
        """工单窗，加上配对要求里没有单独工单的阶段窗。

        配对成员按其阶段窗约束到场：最晚实际到场取窗尾，含当刻即窗尾前 1 秒。
        """
        rows = [dict(row) for row in self.duty.values() if "phase" in row]
        have = {(str(row["event"]), str(row["station"]), str(row["phase"])) for row in rows}
        for item in self.duty.values():
            if "upstream" not in item:
                continue
            event = str(item["event"])
            for side in ("upstream", "downstream"):
                station, phase = str(item[side][0]), str(item[side][1])
                if (event, station, phase) in have:
                    continue
                have.add((event, station, phase))
                span = self.window_span(station, event, phase)
                rows.append({
                    "id": f"PAIR:{item['id']}:{station}:{phase}",
                    "event": event, "station": station, "phase": phase,
                    "arrive_by": show(span[1] - 1) if span else "23:59:59",
                    "from_pair": str(item["id"]),
                })
        return rows

    def windows(self) -> list[WindowResult]:
        results: list[WindowResult] = []
        rows = sorted(self._phase_items(),
                      key=lambda row: (str(row["event"]), str(row["station"]),
                                       str(row["phase"]), str(row["id"])))
        history = self.history()
        for item in rows:
            station, event, phase = str(item["station"]), str(item["event"]), str(item["phase"])
            span = self.window_span(station, event, phase)
            version = history.weather_for(station, event)
            kind, sample, reason = self._pick(station, event, phase)
            result = WindowResult(item, span, "open", reason, sample,
                                  reviewed_with=version.id if version else None)
            if kind == "proven":
                result.status = "proven"
                result.reason = "窗内已有可检的实物样"
            elif kind == "pending":
                result.status = "pending"
            elif kind == "lost":
                if span is not None and span[1] <= seconds(self.now):
                    result.status = "unfillable"
                else:
                    result.status = "open"
                    result.reason += "；窗仍开，尚可另采"
            else:
                if span is not None and span[1] <= seconds(self.now):
                    result.status = "unfillable"
                    result.reason = "窗已关闭且没有可归属的采样"
                elif span is not None and span[0] <= seconds(self.now):
                    result.status = "pending"
                    result.reason = "窗在进行中，尚无证据"
            previous = self.window_seen.get(result.id)
            if previous is not None and previous != result.status:
                self.transitions.append({"at": self.now, "window": result.id,
                                         "from": previous, "to": result.status,
                                         "reason": result.reason})
                self._note("window", f"{result.id} 由 {previous} 更新为 {result.status}：{result.reason}")
            self.window_seen[result.id] = result.status
            results.append(result)
        return results

    def pairs(self) -> list[PairResult]:
        results: list[PairResult] = []
        for item in sorted((row for row in self.duty.values() if "upstream" in row),
                           key=lambda row: str(row["id"])):
            up_member, down_member = item["upstream"], item["downstream"]
            travel_raw = item.get("travel_seconds") or [120, 360]
            travel = (int(travel_raw[0]), int(travel_raw[1]))
            up = self._side(str(item["event"]), str(up_member[0]), str(up_member[1]))
            down = self._side(str(item["event"]), str(down_member[0]), str(down_member[1]))
            result = PairResult(item, "open", "", up, down, travel=travel)
            missing = []
            if up is None or up.status != "proven":
                missing.append(f"{up_member[0]} {up_member[1]}")
            if down is None or down.status != "proven":
                missing.append(f"{down_member[0]} {down_member[1]}")
            if missing:
                states = [side.status for side in (up, down) if side is not None]
                if "unfillable" in states:
                    result.status = "unfillable"
                elif states and all(state in ("pending",) for state in states):
                    result.status = "pending"
                elif "pending" in states:
                    result.status = "pending"
                else:
                    result.status = "open"
                result.reason = "缺 " + "、".join(missing) + " 的可检样"
                results.append(result)
                continue
            assert up is not None and down is not None
            assert up.sample is not None and down.sample is not None
            assert up.sample.possible is not None and down.sample.possible is not None
            spread = Span(down.sample.possible.lo - up.sample.possible.hi,
                          down.sample.possible.hi - up.sample.possible.lo)
            result.spread = spread
            if spread.lo < travel[0] or spread.hi > travel[1]:
                result.status = "unfillable"
                result.reason = (f"上下游可能时差 {spread} 与传递窗 "
                                 f"{show(travel[0])}..{show(travel[1])} 不相容")
            else:
                result.status = "proven"
                result.reason = f"上下游指定阶段各有可检样，时差 {spread} 落在传递窗内"
            results.append(result)
        return results

    def _side(self, event: str, station: str, phase: str) -> WindowResult | None:
        for window in self.windows():
            if (window.station, window.phase) == (station, phase) \
                    and str(window.item["event"]) == event:
                return window
        return None

    def _pair_members(self) -> set[tuple[str, str, str]]:
        members: set[tuple[str, str, str]] = set()
        for row in self.duty.values():
            if "upstream" not in row:
                continue
            for side in ("upstream", "downstream"):
                station, phase = row[side]
                members.add((str(row["event"]), str(station), str(phase)))
        return members

    # -- 出车 -------------------------------------------------------------

    def _radio_outlook(self, station: str, event: str, phase: str, before: int) -> bool:
        """无线补领是否可能在 before 之前把该窗的证据补齐。"""
        slots = self.source.json("/radio/slots")["slots"]
        history = self.history()
        span = self.window_span(station, event, phase)
        for page in self.pages.values():
            if page.station != station or page.events is not None or page.dead is not None:
                continue
            if seconds(page.expires) <= seconds(self.now):
                continue
            if page not in history.live_pages(page.machine):
                continue
            for slot in slots:
                if slot["channel"] != page.channel:
                    continue
                if seconds(str(slot["from"])) < before \
                        and seconds(str(slot["until"])) > seconds(self.now):
                    if self._spent(slot) < int(slot["pages"]):
                        if span is None or seconds(page.ready) < span[1]:
                            return True
        return False

    def _booked_windows(self) -> set[str]:
        return {booking.window for booking in self.bookings.values() if booking.live}

    def _slot_taken(self, trip: str, slot: str) -> bool:
        return any(booking.trip == trip and booking.bottle_set == slot and booking.live
                   for booking in self.bookings.values())

    def plan_trips(self) -> list[dict[str, Any]]:
        """候选出车与"现在要不要占车"的判断。

        一个窗要进候选，得同时成立：尚未判死、还没有在办任务、工单最晚到场未过、
        某车次的到站时刻还落在指定阶段窗内且不晚于 arrive_by、该车次预约角未过、
        车上还有空箱位。

        排序：配对成员窗（group 0）先挑车和箱位，独立窗（group 1）只用剩下的；同组
        内先让无线已无路可走的窗、再按最晚到场、最后按车次截止。动作：

        * ``book``——该占车了：末班车且（预约角将截止，或无线已救不了，或这是缺一
          侧、又已无补领路径的配对成员窗）；
        * ``wait_radio``——缺的样仍可能在出车前靠补领页解决，先不占车；
        * 不列出——已判死、已占车，或已没有能办该窗的车次。
        """
        members = self._pair_members()
        booked = self._booked_windows()
        now = seconds(self.now)
        windows = {window.id: window for window in self.windows()}
        options: dict[str, list[dict[str, Any]]] = {}
        for window in windows.values():
            if window.status in ("proven", "unfillable") or window.id in booked:
                continue
            if window.arrive_by is None or seconds(window.arrive_by) < now:
                continue
            # 窗的实物时刻还没到，但车可能得提前占：只要今班还有车能在窗内到场就先算，
            # 否则末班车会在窗开之前悄悄滑走。
            for trip in self.trips.values():
                if seconds(trip.book_by) < now:
                    continue
                arrival = trip.stations.get(window.station)
                if arrival is None or seconds(arrival) > seconds(window.arrive_by):
                    continue
                if window.span is not None and not (
                        window.span[0] <= seconds(arrival) < window.span[1]):
                    continue
                free = [slot for slot in trip.box_slots
                        if not self._slot_taken(trip.trip, slot)]
                if not free:
                    continue
                key = (str(window.item["event"]), window.station, window.phase)
                options.setdefault(window.id, []).append({
                    "window": window.id, "event": window.item["event"],
                    "station": window.station, "phase": window.phase,
                    "window_status": window.status, "arrive_by": window.arrive_by,
                    "trip": trip.trip, "bottle_set": free[0], "arrival": arrival,
                    "book_by": trip.book_by,
                    "group": 0 if key in members else 1,
                })

        plans: list[dict[str, Any]] = []
        for window_id, choices in sorted(options.items()):
            choices.sort(key=lambda row: (seconds(row["book_by"]), row["trip"]))
            last = choices[-1]                       # 还能办该窗的末班车
            window = windows[window_id]
            radio = self._radio_outlook(window.station, str(window.item["event"]),
                                        window.phase, seconds(last["arrival"]))
            last_call = seconds(last["book_by"]) - now <= LAST_CALL
            # 配对成员窗要整组凑齐才有意义：缺的一侧若已无补领路径可走，就把车位
            # 先占住，别等末班车开走。仍可能靠页补齐的成员窗先把车位让给没退路的。
            urgent = any((str(window.item["event"]), window.station, window.phase) == member
                         for member in members) and not radio
            for rank, choice in enumerate(choices):
                if self._slot_taken(choice["trip"], choice["bottle_set"]):
                    continue
                # 末班车：无线救不了、已到最后关头、或配对窗已在流失，就占位。
                action = ("book" if choice["trip"] == last["trip"]
                          and (last_call or not radio or urgent) else "wait_radio")
                if action == "book" and any(row["window"] == window_id and row["action"] == "book"
                                            for row in plans):
                    action = "wait_radio"
                plans.append({**choice, "action": action, "radio_possible": radio,
                              "last_call": last_call, "urgent": urgent,
                              "priority": [choice["group"], 0 if not radio else 1,
                                           min(seconds(choice["arrive_by"]),
                                               window.span[1] if window.span else DAY),
                                           seconds(choice["book_by"]), rank, choice["trip"]]})
        # 配对成员窗先挑车和箱位；再让独立窗只用剩下的。
        plans.sort(key=lambda row: (row["group"], row["priority"]))
        chosen: list[dict[str, Any]] = []
        used: set[tuple[str, str]] = set()
        seated: set[str] = set()
        for plan in plans:
            if plan["window"] in seated:
                continue
            if plan["action"] == "wait_radio":
                chosen.append(plan)
                continue
            key = (plan["trip"], plan["bottle_set"])
            if key in used:
                continue
            used.add(key)
            seated.add(plan["window"])
            chosen.append(plan)
        return chosen

    def book(self, plan: Mapping[str, Any]) -> Booking:
        request = f"{plan['event']}-{plan['window']}-{plan['trip']}-{plan['bottle_set']}"
        old = self.bookings.get(request)
        if old is not None:
            # 同请求号只允许同内容；已经在途或已接的单不再重下。
            if old.state in ("in_flight", "accepted"):
                return old
        booking = Booking(request, str(plan["window"]), str(plan["trip"]), str(plan["station"]),
                          str(plan["bottle_set"]), str(plan["event"]), str(plan["phase"]),
                          self.now)
        status, _, body = self.source.request("/trip/book", payload=booking.travel())
        payload = json.loads(body) if body else {}
        if status == 200:
            booking.state = "accepted"
            booking.receipt = payload
        elif status == 503:
            # 回执可能已丢；保留在途占用，不回滚、不重下。
            booking.state = "in_flight"
            booking.note = "提交后回执丢失，保留在途占用"
        elif status == 409:
            if payload.get("state") == "box_slot_in_use":
                booking.state = "rejected"
                booking.note = "箱位已被另一任务占用"
            else:
                booking.state = "rejected"
                booking.note = "同请求号已有不同内容"
        elif status in (422, 400):
            booking.state = "rejected"
            booking.note = str(payload.get("state"))
        else:
            booking.state = "unreachable"
            booking.note = f"HTTP {status}"
        self.bookings[request] = booking
        self._note("trip", f"{booking.state} {booking.trip}/{booking.station}/{booking.bottle_set}")
        return booking

    def book_planned(self, plans: Iterable[Mapping[str, Any]] | None = None) -> list[Booking]:
        new = []
        for plan in (plans if plans is not None else self.plan_trips()):
            if plan["action"] != "book":
                continue
            # 同一窗只占一次；箱位是否被占由 book() 依服务端回执判定，并保留在途尝试。
            if any(booking.window == plan["window"] and booking.live
                   for booking in self.bookings.values()):
                continue
            new.append(self.book(plan))
        return new

    def confirm_bookings(self) -> None:
        for booking in self.bookings.values():
            if booking.state != "in_flight":
                continue
            status, _, body = self.source.request("/trip/receipt", {"request": booking.request})
            if status == 200:
                booking.state = "accepted"
                booking.receipt = json.loads(body)
                self._note("trip", f"{booking.request} 回执确认")
            # 404 保持 in_flight：回执未确认前不重下、不撤占。

    def trip_report(self) -> list[dict[str, Any]]:
        rows = []
        for trip in self.trips.values():
            live = [booking for booking in self.bookings.values()
                    if booking.trip == trip.trip and booking.live]
            fields = []
            for window in self.windows():
                arrival = trip.stations.get(window.station)
                if arrival is None:
                    continue
                fields.append({
                    "window": window.id, "station": window.station, "phase": window.phase,
                    "status": window.status, "arrive_by": window.arrive_by,
                    "arrival": arrival,
                    "fits": seconds(arrival) <= seconds(window.arrive_by)
                    and seconds(trip.book_by) >= seconds(self.now),
                })
            rows.append({"trip": trip.trip, "book_by": trip.book_by,
                         "stations": dict(trip.stations), "box_slots": list(trip.box_slots),
                         "bookable": seconds(trip.book_by) >= seconds(self.now),
                         "open": seconds(trip.book_by) >= seconds(self.now),
                         "bookings": [{**booking.travel(), "state": booking.state,
                                       "receipt": booking.receipt} for booking in live],
                         "windows": fields})
        return rows

    # -- 报告 -------------------------------------------------------------

    def close(self) -> None:
        """本对象不持有长连接，留着给调用方与测试显式收尾。"""
        return None

    def status(self) -> dict[str, Any]:
        windows = self.windows()
        pairs = self.pairs()
        by_station: dict[str, dict[str, Any]] = {}
        for station in self.stations():
            samples = self.station_samples(station)
            by_station[station] = {
                "station": station,
                "proven": sum(1 for sample in samples if sample.state == "proven"),
                "pending": sum(1 for sample in samples if sample.state == "pending"),
                "lost": sum(1 for sample in samples if sample.state == "lost"),
                "samples": [sample.as_dict() for sample in samples],
            }
        return {
            "now": self.now,
            "stations": [by_station[station] for station in sorted(by_station)],
            "windows": [window.as_dict() for window in windows],
            "pairs": [pair.as_dict() for pair in pairs],
            "trips": self.trip_report(),
            "plans": self.plan_trips(),
            "archives": list(self.archives.values()),
            "transitions": list(self.transitions),
            "notes": list(self.notes),
        }

    def report(self, event: str | None = None) -> dict[str, Any]:
        """值班员按事件查：每对断面缺什么、哪些车次能办、哪些窗已无法补齐。"""
        status = self.status()
        windows = [row for row in status["windows"] if event is None or row["event"] == event]
        pairs = [row for row in status["pairs"] if event is None or row["event"] == event]
        involved = {row["station"] for row in windows}
        trips = []
        for trip in status["trips"]:
            relevant = [row for row in trip["windows"]
                        if event is None or row["window"] in {item["id"] for item in windows}]
            trips.append({**trip, "windows": relevant})
        return {
            "now": status["now"],
            "event": event,
            "pairs": pairs,
            "windows": windows,
            "unfillable": [row for row in windows if row["status"] == "unfillable"],
            "trips": [trip for trip in trips
                      if trip["open"] or trip["bookings"]],
            "plans": [row for row in status["plans"]
                      if event is None or row["event"] == event],
            "archives": [row for row in status["archives"]
                         if event is None or row["station"] in involved],
            "notes": status["notes"],
            "transitions": status["transitions"],
        }


def summarize(report: Mapping[str, Any]) -> str:
    """把报告排成值班屏上能一眼看懂的几行。"""
    lines = [f"当前 {report['now']}" + (f"  事件 {report['event']}" if report.get("event") else "")]
    for pair in report["pairs"]:
        up, down = pair["upstream"], pair["downstream"]
        detail = []
        for side in (up, down):
            if side is None:
                continue
            detail.append(f"{side['station']} {side['phase']}={side['status']}"
                          + (f"（{side['reason']}）" if side["status"] != "proven" else ""))
        lines.append(f"配对 {pair['id']}: {pair['status']}  " + "  ".join(detail)
                     + (f"  时差 {pair['spread']}" if pair.get("spread") else ""))
        if pair["status"] != "proven":
            lines.append(f"    缺: {pair['reason']}")
    for window in report["windows"]:
        if window["status"] == "proven":
            sample = window["sample"] or {}
            label = "/".join(sample.get("bottle") or []) if sample.get("bottle") else ""
            tail = f"  瓶 {label}" if label else ""
            if sample.get("lab") == "received":
                tail += f"  已送检 {sample.get('lab_receipt')}"
            lines.append(f"窗 {window['id']} {window['station']} {window['phase']}: 已证{tail}")
        else:
            lines.append(f"窗 {window['id']} {window['station']} {window['phase']}: "
                         f"{window['status']}  {window['reason']}")
    for plan in report.get("plans", []):
        mark = {"book": "现在占车", "wait_radio": "等无线补领"}.get(plan["action"], plan["action"])
        lines.append(f"可办 {plan['window']} {plan['station']} {plan['phase']}: {mark} "
                     f"（{plan['trip']}/{plan['bottle_set']}，到站 {plan['arrival']}）")
    for trip in report["trips"]:
        state = "可约" if trip["bookable"] else "截止"
        active = "、".join(f"{row['station']}/{row['bottle_set']}/{row['state']}"
                           for row in trip["bookings"]) or "无任务"
        lines.append(f"车 {trip['trip']}: {state}  在办 {active}")
    unfillable = [row["id"] for row in report.get("unfillable", [])]
    if unfillable:
        lines.append("已无法补齐: " + "、".join(unfillable))
    for row in report.get("archives", []):
        basis = "、".join(dict.fromkeys(item["id"] for item in row.get("versions", []))) or "无"
        physical = row.get("physical")
        label = (f"{physical[0]}/{physical[1]}" if physical
                 else f"{row['box']}/{row.get('bottle') or '?'}")
        lines.append(f"送检 {row['station']} {label} {row['state']}"
                     f"  凭据 {basis}  存证 {str(row.get('digest', ''))[:10]}")
    return "\n".join(lines)


