from __future__ import annotations

import workflow_runner


flow = workflow_runner.load_module("battle_protocol_flow_v4")


class _FakeCookies:
    def __init__(self) -> None:
        self.jar = self

    def __iter__(self):
        return iter(())

    def set(self, *args, **kwargs):
        return None


class _FakeResponse:
    def __init__(self, text: str, url: str) -> None:
        self.status_code = 200
        self.url = url
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {"content-type": "text/html"}


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.headers = {}
        self.cookies = _FakeCookies()
        self.response = response

    def request(self, method: str, url: str, **kwargs):
        return self.response


def _flow_form(step: str) -> str:
    return f"""
    <form id="flow-form" method="POST"
          action="https://account.battle.net/creation/flow/creation-full/step/{step}">
      <input type="hidden" name="_csrf" value="csrf-token">
    </form>
    """


def test_bootstrap_accepts_row_redirect_to_tassadar(tmp_path) -> None:
    entry_url = "https://account.battle.net/creation/flow/creation-full"
    state = flow.PersistentFlowState.create(
        tmp_path / "state.json",
        identity={"email": "mail@example.com"},
    )
    response = _FakeResponse(
        _flow_form("row-redirect-to-tassadar"),
        entry_url,
    )
    client = flow.BattleProtocolClient(
        state,
        tmp_path,
        entry_url=entry_url,
        session=_FakeSession(response),
    )

    form = client.bootstrap()

    assert form.step == "row-redirect-to-tassadar"
    assert state.data["form"]["step"] == "row-redirect-to-tassadar"
