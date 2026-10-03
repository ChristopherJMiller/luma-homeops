from state_fixtures import FakeHA, snapshot

from hactl.state import live


class Recorder(FakeHA):
    def __init__(self, snap):
        super().__init__(snap)
        self.option_reads = []

    def post(self, path, data=None, raw=False):
        self.option_reads.append(data["handler"])
        return {"type": "form", "flow_id": "o1", "step_id": "cover",
                "data_schema": [{"name": "hide_members", "default": False}]}

    def rest(self, method, path, data=None, raw=False, timeout=30):
        return {"message": "Flow aborted"}


def test_fetch_reads_every_collection():
    snap = live.fetch(FakeHA(snapshot()))
    assert [a["area_id"] for a in snap.areas] == ["bedroom", "kitchen"]
    assert len(snap.devices) == 3 and len(snap.entries) == 3 and len(snap.resources) == 1
    assert snap.options == {}


def test_fetch_reads_options_only_when_wanted():
    fake = Recorder(snapshot())
    snap = live.fetch(fake, want_options={("group", "Bedroom Blinds")})
    assert fake.option_reads == ["e1"]
    assert snap.options[("group", "Bedroom Blinds")] == {"step_id": "cover", "values": {"hide_members": False}}
