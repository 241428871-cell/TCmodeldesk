"""Native-only shutdown adapter; imports never start a service."""
from __future__ import annotations

import sys
import threading
from pathlib import Path


def serve(script: Path) -> None:
    """Adapt the existing server; native transport adds no model or clock."""
    sys.path.insert(0, str(script.parent))
    import panel_server as panel
    gate = threading.RLock()
    class DesktopHandler(panel.Handler):
        closing = False

        def do_POST(self) -> None:
            with gate:
                if self.closing:
                    self.send_json({'error': '工作台正准备退出或迁移，请稍后重新打开'}, 503)
                    return
                if self.path == '/api/desktop/shutdown':
                    try:
                        self.guard(mutation=True)
                        if self.headers.get('Content-Length', '0') not in ('0', '2'):
                            raise ValueError('退出操作不接受额外参数')
                        if any(row.get('status') == panel.stage_store.GOAL_ACTIVE for row in panel.stage_store.list_goals()):
                            raise ValueError('仍有启用的目标，请暂停目标后退出或迁移数据')
                        for goal in panel.stage_store.list_goals():
                            if goal.get('project') and panel.continuation_clock.snapshot(goal['project'], goal['id']).get('active_intent'):
                                raise ValueError('原主脑轮次尚未结算，请核对后退出或迁移')
                        panel.cli_source_idle_guard()
                        DesktopHandler.closing = True
                        self.send_json({'ok': True})
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
                    except (ValueError, OSError, PermissionError) as error:
                        self.send_json({'error': str(error)}, 409)
                    return
                super().do_POST()

    panel.Handler = DesktopHandler
    panel.DESKTOP_MANAGED = True
    panel.main()

