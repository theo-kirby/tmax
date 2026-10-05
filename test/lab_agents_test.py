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
