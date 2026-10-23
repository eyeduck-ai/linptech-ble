from linptech_ble.auto_connect import AutoConnector


class Controller:
    def __init__(self):
        self.state = {"token_configured": True, "authenticated": False, "busy": False, "last_operation": None}
        self.commands = []

    def snapshot(self):
        return self.state

    def submit(self, command):
        self.commands.append(command)


def test_retries_are_bounded_and_require_scanner_ready():
    controller = Controller()
    readiness = [False]
    auto = AutoConnector(controller, ready=lambda: readiness[0])
    auto.tick(0)
    assert not controller.commands
    readiness[0] = True
    auto.tick(1)
    auto.tick(2)
    assert controller.commands == [{"action": "connect"}]
    auto.tick(31)
    assert len(controller.commands) == 2


def test_manual_disconnect_suspends_retries_until_explicit_connect():
    controller = Controller()
    auto = AutoConnector(controller)
    controller.state["last_operation"] = {"action": "disconnect", "status": "done"}
    auto.tick(0)
    assert not controller.commands
    controller.state["last_operation"] = {"action": "connect", "status": "error"}
    auto.tick(1)
    assert controller.commands == [{"action": "connect"}]


def test_never_reconnect_authenticated_busy_or_unconfigured_controller():
    for key in ("authenticated", "busy", "token_configured"):
        controller = Controller()
        controller.state[key] = key != "token_configured"
        AutoConnector(controller).tick(0)
        assert not controller.commands
    controller = Controller()
    auto = AutoConnector(controller)
    auto.stop()
    auto.tick(0)
    assert not controller.commands
