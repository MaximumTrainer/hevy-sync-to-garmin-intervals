"""Write strength.fit, the small Garmin-style strength recording the tests decode.

The file has what a watch writes for a strength session: one `record` a second
with heart rate, and one `set` message per set (active or rest) with its start
time, duration, rep count and exercise category.

    pip install fit-tool
    python tests/fixtures/make_strength_fit.py
"""
from datetime import datetime, timezone
from pathlib import Path

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.messages.set_message import SetMessage
from fit_tool.profile.profile_type import FileType, Manufacturer, SetType, Sport

START = datetime(2026, 10, 27, 7, 8, 38, tzinfo=timezone.utc)
# (seconds from start, duration, active?, reps, FIT exercise category)
SETS = [
    (5, 30, True, 8, 0),     # bench press (category 0)
    (35, 60, False, None, None),
    (95, 30, True, 8, 0),
    (125, 60, False, None, None),
    (185, 30, True, 8, 23),  # row (category 23)
    (215, 45, False, None, None),
    (260, 45, True, 0, 6),   # crunch (category 6): the hollow hold
]
LENGTH = 320


def ms(seconds: int) -> int:
    return round((START.timestamp() + seconds) * 1000)


def main() -> None:
    b = FitFileBuilder(auto_define=True, min_string_size=50)
    fid = FileIdMessage()
    fid.type = FileType.ACTIVITY
    fid.manufacturer = Manufacturer.GARMIN.value
    fid.time_created = ms(0)
    b.add(fid)
    for t in range(LENGTH):
        r = RecordMessage()
        r.timestamp = ms(t)
        r.heart_rate = 90 + (40 if any(a and s <= t < s + d for s, d, a, _, _ in SETS) else 0)
        b.add(r)
    for i, (s, d, active, reps, cat) in enumerate(SETS):
        m = SetMessage()
        m.message_index = i
        m.timestamp = ms(s + d)
        m.start_time = ms(s)
        m.duration = float(d)
        m.set_type = SetType.ACTIVE.value if active else SetType.REST.value
        if reps is not None:
            m.repetitions = reps
        if cat is not None:
            m.category = [cat]
        b.add(m)
    sess = SessionMessage()
    sess.timestamp = ms(LENGTH)
    sess.start_time = ms(0)
    sess.sport = Sport.TRAINING.value
    sess.total_elapsed_time = float(LENGTH)
    b.add(sess)
    b.build().to_file(str(Path(__file__).with_name("strength.fit")))


if __name__ == "__main__":
    main()
