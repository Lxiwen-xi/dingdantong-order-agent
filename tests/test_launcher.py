from unittest.mock import Mock

import run


def use_current_interpreter(monkeypatch):
    monkeypatch.setattr(run.sys, 'prefix', str(run.ROOT / '.venv'))


def test_existing_service_opens_page_without_starting_duplicate(monkeypatch, capsys):
    use_current_interpreter(monkeypatch)
    monkeypatch.setattr(run, 'port_in_use', lambda port: True)
    monkeypatch.setattr(run, 'service_ready', lambda url: True)
    open_browser = Mock(return_value=True)
    monkeypatch.setattr(run.webbrowser, 'open_new_tab', open_browser)
    start = Mock()
    monkeypatch.setattr(run.subprocess, 'Popen', start)
    assert run.main() == 0
    open_browser.assert_called_once_with('http://127.0.0.1:8602')
    start.assert_not_called()
    assert '网页服务仍在运行' in capsys.readouterr().out


def test_first_start_waits_for_health_then_opens_browser(monkeypatch):
    use_current_interpreter(monkeypatch)
    monkeypatch.setattr(run, 'port_in_use', lambda port: False)
    monkeypatch.setattr(run, 'service_ready', Mock(side_effect=[False, True]))
    monkeypatch.setattr(run.time, 'sleep', lambda seconds: None)
    process = Mock()
    process.poll.side_effect = [None, 0]
    process.wait.return_value = 0
    monkeypatch.setattr(run.subprocess, 'Popen', Mock(return_value=process))
    browser = Mock(return_value=True)
    monkeypatch.setattr(run.webbrowser, 'open_new_tab', browser)
    assert run.main() == 0
    browser.assert_called_once()
    process.terminate.assert_not_called()


def test_port_conflict_does_not_open_unrelated_page(monkeypatch):
    use_current_interpreter(monkeypatch)
    monkeypatch.setattr(run, 'port_in_use', lambda port: True)
    monkeypatch.setattr(run, 'service_ready', lambda url: False)
    browser = Mock()
    monkeypatch.setattr(run.webbrowser, 'open_new_tab', browser)
    assert run.main() == 1
    browser.assert_not_called()


def test_browser_failure_leaves_clickable_address(monkeypatch, capsys):
    monkeypatch.setattr(run.webbrowser, 'open_new_tab', Mock(side_effect=OSError('no browser')))
    run.open_page('http://127.0.0.1:8602')
    output = capsys.readouterr().out
    assert 'http://127.0.0.1:8602' in output
    assert '未能自动打开浏览器' in output
