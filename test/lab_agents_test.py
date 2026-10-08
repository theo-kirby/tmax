import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import lab_agents as agents
import agent_status

class ReporterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.cfg=self.root/"config.json";self.cfg.write_text(json.dumps({"host":"fixture"}))
        self.env=patch.dict(os.environ,{"TMAX_AGENT_STATE":str(self.root/"state"),"TMAX_AGENT_CONFIG":str(self.cfg)})
        self.env.start()
    def tearDown(self):self.env.stop();self.temp.cleanup()
    def test_idle_is_not_attention_and_approval_is(self):
        self.assertEqual(agents.event_activity("Stop",{}),("idle",""))
        self.assertEqual(agents.event_activity("PermissionRequest",{}),("waiting","approval"))
        self.assertEqual(agents.event_activity("PreToolUse",{"tool_name":"request_user_input"}),("waiting","answer"))
        self.assertEqual(agents.event_activity("Interrupt",{}),("idle",""))
    def test_hooks_keep_run_identity_but_pid_reuse_is_a_new_attempt(self):
        agents.hook("codex","SessionStart",{"session_id":"thread"},(100,"first"))
        agents.hook("codex","UserPromptSubmit",{"session_id":"thread"},(100,"first"))
        agents.hook("codex","SessionStart",{"session_id":"thread"},(100,"second"))
        values=agents.snapshot();self.assertEqual(len(values),2);self.assertEqual(values[0]["runId"],values[1]["runId"]);self.assertNotEqual(values[0]["id"],values[1]["id"])
    def test_hook_preserves_explicit_runner_parent(self):
        sid=agents.identity("session","codex",100,"first")
        value=agents.base(sid,"fixture:runner","codex","test")
        value.update(parentId="fixture:parent",runner="ouroboros")
        agents.record(value,pid=100,started="first")
        agents.hook("codex","UserPromptSubmit",{"session_id":"thread"},(100,"first"))
        self.assertEqual(agents.snapshot()[0]["runId"],"fixture:runner")
    def test_no_heartbeat_history_and_network_failure_keeps_queue(self):
        sid=agents.identity("test");value=agents.base(sid,sid,"codex","test")
        agents.record(value);value["observedAt"]=agents.now();agents.record(value)
        with agents.database() as db:self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0],1)
        (self.root/"token").write_text("fixture-secret")
        self.cfg.write_text(json.dumps({"host":"fixture","endpoint":"https://example.invalid/api/agents/ingest","tokenFile":str(self.root/"token")}))
        with patch("urllib.request.urlopen",side_effect=OSError("offline")):
            with self.assertRaises(OSError):agents.publish()
        with agents.database() as db:self.assertEqual(db.execute("SELECT sent FROM events").fetchone()[0],0)
    def test_exit_and_proxy_deduplication(self):
        agents.hook("codex","UserPromptSubmit",{},(100,"first"))
        with patch.object(agent_status,"processes",return_value={}),patch.object(agent_status,"run",return_value="%1\t55\tproxy\t/tmp\tsb1x\t/tmp/socket"):
            agents.collect_interactive()
        values=agents.snapshot();self.assertEqual(len(values),1);self.assertEqual(values[0]["activity"],"stopped");self.assertEqual(values[0]["attention"],"review exit")
    def test_surviving_process_loses_deleted_tmux_location(self):
        sid=agents.identity("session","claude",100,"first")
        value=agents.base(sid,sid,"claude","process observation")
        value.update(activity="idle",attachment={"host":"fixture","socket":"/tmp/socket","session":"embed","pane":"%1"})
        agents.record(value,pid=100,started="first")
        with patch.object(agent_status,"processes",return_value={100:(1,"first","claude"),55:(1,"shell","zsh")}),patch.object(agent_status,"run",return_value="%2\t55\tlab\t/tmp\t\t/tmp/socket"):
            agents.collect_interactive()
        current=agents.snapshot()[0]
        self.assertIsNone(current["attachment"]);self.assertEqual(current["activity"],"idle")
        with agents.database() as db:self.assertEqual(json.loads(db.execute("SELECT payload FROM events ORDER BY seq DESC LIMIT 1").fetchone()[0])["kind"],"location_lost")
    def test_failed_scan_does_not_refresh_old_location(self):
        sid=agents.identity("session","codex",100,"first")
        value=agents.base(sid,sid,"codex","process observation")
        value.update(observedAt="2020-01-01T00:00:00Z",attachment={"host":"fixture","socket":"/tmp/socket","session":"old","pane":"%1"})
        agents.record(value,pid=100,started="first")
        with patch.object(agent_status,"processes",return_value={100:(1,"first","codex")}),patch.object(agent_status,"run",side_effect=OSError("unavailable")):
            agents.collect_interactive()
        self.assertEqual(agents.snapshot()[0]["observedAt"],value["observedAt"])
    def test_hook_rejects_inherited_missing_or_unrelated_pane(self):
        with patch.dict(os.environ,{"TMUX":"/tmp/socket,1,0"}):
            with patch.object(agent_status,"run",return_value="lab\t55"),patch.object(agent_status,"processes",return_value={100:(55,"first","codex"),55:(1,"shell","zsh")}):
                agents.hook("codex","UserPromptSubmit",{},(100,"first"),"%1")
            self.assertEqual(agents.snapshot()[0]["attachment"]["session"],"lab")
            with patch.object(agent_status,"run",return_value="lab\t55"),patch.object(agent_status,"processes",return_value={100:(1,"first","codex"),55:(1,"shell","zsh")}):
                agents.hook("codex","UserPromptSubmit",{},(100,"first"),"%1")
            self.assertIsNone(agents.snapshot()[0]["attachment"])
            with patch.object(agent_status,"run",return_value=""):
                agents.hook("codex","UserPromptSubmit",{},(100,"first"),"%1")
            self.assertIsNone(agents.snapshot()[0]["attachment"])
    def test_terminal_working_overrides_old_idle_hook_and_unknown_recovers(self):
        table={55:(1,"shell","zsh"),100:(55,"first","codex")}
        agents.hook("codex","Stop",{},(100,"first"))
        def output(cmd):
            return "• Working (1m • esc to interrupt)" if "capture-pane" in cmd else "%1\t55\tlab\t/tmp\t\t/tmp/socket"
        with patch.object(agent_status,"processes",return_value=table),patch.object(agent_status,"run",side_effect=output),patch.object(agent_status,"codex_backend_links",return_value={}):
            agents.collect_interactive()
        value=agents.snapshot()[0];self.assertEqual(value["activity"],"working");self.assertEqual(value["source"],"terminal indicator")
        with patch.object(agent_status,"processes",return_value=table),patch.object(agent_status,"run",return_value="%1\t55\tlab\t/tmp\t\t/tmp/socket"),patch.object(agent_status,"codex_backend_links",return_value={}):agents.collect_interactive()
        self.assertEqual(agents.snapshot()[0]["activity"],"unknown")
    def test_background_work_survives_stop_and_clears_when_finished(self):
        agents.hook("claude","Stop",{},(100,"first"))
        table={55:(1,"shell","zsh"),100:(55,"first","claude")}
        footer="❯\n⏵⏵ bypass permissions on · 2 shells · ← for agents"
        def output(cmd):return footer if "capture-pane" in cmd else "%1\t55\tlab\t/tmp\t\t/tmp/socket"
        with patch.object(agent_status,"processes",return_value=table),patch.object(agent_status,"run",side_effect=output),patch.object(agent_status,"codex_backend_links",return_value={}):
            agents.collect_interactive()
            value=agents.snapshot()[0]
            self.assertEqual(value["activity"],"working")
            self.assertEqual(value["attention"],"")
            self.assertEqual(value["summary"],"Waiting for background work")
            footer="❯\n⏵⏵ bypass permissions on"
            agents.collect_interactive()
            self.assertEqual(agents.snapshot()[0]["activity"],"idle")
            self.assertEqual(agents.snapshot()[0]["summary"],"Ready for input")

    def test_ready_prompt_cannot_override_recent_working_event(self):
        agents.hook("claude","PreToolUse",{},(100,"first"))
        table={55:(1,"shell","zsh"),100:(55,"first","claude")}
        def output(cmd):return "❯\nshift+tab to cycle" if "capture-pane" in cmd else "%1\t55\tlab\t/tmp\t\t/tmp/socket"
        with patch.object(agent_status,"processes",return_value=table),patch.object(agent_status,"run",side_effect=output),patch.object(agent_status,"codex_backend_links",return_value={}):agents.collect_interactive()
        value=agents.snapshot()[0];self.assertEqual(value["activity"],"working");self.assertEqual(value["source"],"provider hook")
        value["lastHook"]["at"]="2020-01-01T00:00:00Z";agents.record(value,pid=100,started="first")
        with patch.object(agent_status,"processes",return_value=table),patch.object(agent_status,"run",side_effect=output),patch.object(agent_status,"codex_backend_links",return_value={}):agents.collect_interactive()
        self.assertEqual(agents.snapshot()[0]["activity"],"unknown")

    def test_idle_notification_does_not_hide_failure(self):
        agents.hook("claude","StopFailure",{},(100,"first"))
        agents.hook("claude","Notification",{"notification_type":"idle_prompt"},(100,"first"))
        self.assertEqual(agents.snapshot()[0]["attention"],"review failure")
        agents.hook("claude","Notification",{"notification_type":"unrelated"},(100,"first"))
        self.assertEqual(agents.snapshot()[0]["attention"],"review failure")
        agents.hook("claude","UserPromptSubmit",{},(100,"first"))
        self.assertEqual(agents.snapshot()[0]["attention"],"")
    def test_backend_link_is_metadata_not_a_guessed_tmux_attachment(self):
        agents.hook("codex","UserPromptSubmit",{},(200,"server"))
        table={55:(1,"shell","zsh"),100:(55,"first","codex"),200:(1,"server","codex app-server")}
        def output(cmd):return "Working (esc to interrupt)" if "capture-pane" in cmd else "%1\t55\tlab\t/tmp\t\t/tmp/socket"
        with patch.object(agent_status,"processes",return_value=table),patch.object(agent_status,"run",side_effect=output),patch.object(agent_status,"codex_backend_links",return_value={200:100}):agents.collect_interactive()
        backend=next(v for v in agents.snapshot() if v.get("role")=="backend")
        self.assertIsNone(backend["attachment"]);self.assertEqual(backend["parentId"],agents.identity("session","codex",100,"first"))

    def test_linux_codex_native_binary(self):
        self.assertEqual(agent_status.agent_name("/opt/codex-x86_64-unknown-linux-gnu"),"codex")
    def test_completed_runner_backfills_actor_failure_after_reporter_upgrade(self):
        project=self.root/"failed";run=project/".ouroboros/runs/test";run.mkdir(parents=True)
        self.cfg.write_text(json.dumps({"host":"fixture","ouroboros":[str(project)]}))
        (run/"status.json").write_text(json.dumps({"state":"stopped","iteration":1,"stop_reason":"budget"}))
        (run/"pid").write_text("101")
        (run/"iterations.jsonl").write_text(json.dumps({"ts":agents.now(),"step":"actor","exit":-5,"error":"crash","iteration":1})+"\n")
        with patch.object(agent_status,"processes",return_value={}),patch.object(agent_status,"run",return_value=""):
            agents.collect_ouroboros()
        self.assertEqual(agents.snapshot()[0]["attention"],"review failure")
    def test_runner_status_history_and_restart(self):
        project=self.root/"project";run=project/".ouroboros/runs/test";run.mkdir(parents=True)
        self.cfg.write_text(json.dumps({"host":"fixture","ouroboros":[str(project)]}))
        (run/"status.json").write_text(json.dumps({"state":"work","iteration":1,"last_ok_tag":"ok-1"}))
        (run/"pid").write_text("101")
        (run/"iterations.jsonl").write_text(json.dumps({"ts":agents.now(),"step":"critique","verdict":"accept","iteration":1})+"\n")
        with patch.object(agent_status,"processes",return_value={101:(1,"first","python ouroboros run")}),patch.object(agent_status,"run",return_value=""):
            agents.collect_ouroboros();agents.collect_ouroboros()
        with agents.database() as db:
            rows=db.execute("SELECT payload FROM events").fetchall();self.assertEqual(sum(json.loads(r[0])["kind"]=="critique" for r in rows),1)
        with patch.object(agent_status,"processes",return_value={101:(1,"second","python ouroboros run")}),patch.object(agent_status,"run",return_value=""):
            agents.collect_ouroboros()
        values=agents.snapshot();self.assertEqual(len(values),2);self.assertEqual(values[0]["runId"],values[1]["runId"])

if __name__=="__main__":unittest.main()
