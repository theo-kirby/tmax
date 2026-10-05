#!/usr/bin/env python3
"""Private metadata reporter. Local hooks never wait for the network."""
import argparse
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def state_dir():
    return Path(os.environ.get("TMAX_AGENT_STATE", str(Path.home()/".local/state/tmax-agents")))


def config():
    path=Path(os.environ.get("TMAX_AGENT_CONFIG", str(Path.home()/".config/tmax/agents.json")))
    try:return json.loads(path.read_text())
    except FileNotFoundError:return {}


def host_id():
    host=config().get("host",socket.gethostname().split(".")[0])
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}",host):raise ValueError("Invalid host identity")
    return host


def identity(*parts):
    return host_id()+":"+hashlib.sha256("\0".join(map(str,parts)).encode()).hexdigest()[:32]


@contextlib.contextmanager
def database():
    root=state_dir();root.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=root/"events.sqlite"
    db=sqlite3.connect(path,timeout=2);path.chmod(0o600)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,payload TEXT NOT NULL,sent INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,payload TEXT NOT NULL,signature TEXT NOT NULL,pid INTEGER,started TEXT);
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);""")
    try:
        with db: yield db
    finally: db.close()


def record(value,kind="state",pid=None,started=None,force=False):
    # Only bounded, purpose-built metadata is accepted; never raw hook payloads.
    encoded=json.dumps({k:v for k,v in value.items() if k!="observedAt"},sort_keys=True)
    with database() as db:
        old=db.execute("SELECT signature,pid,started FROM sessions WHERE id=?",(value["id"],)).fetchone()
        if force or not old or old[0]!=encoded:
            eid=host_id()+":"+uuid.uuid4().hex
            db.execute("INSERT INTO events(id,payload) VALUES(?,?)",(eid,json.dumps({"kind":kind,"session":value})))
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,signature=excluded.signature,pid=COALESCE(excluded.pid,sessions.pid),started=COALESCE(excluded.started,sessions.started)",(value["id"],json.dumps(value),encoded,pid,started))


def base(sid,run_id,provider,source):
    return dict(id=sid,runId=run_id,host=host_id(),provider=provider,runner="",activity="unknown",attention="",summary="",project="",role="",parentId="",observedAt=now(),source=source,attachment=None,evidence="",iteration=0,stopReason="",usage="")


def event_activity(event,payload):
    tool=str(payload.get("tool_name","")).split(".")[-1].lower()
    if event in ("SessionEnd","session_shutdown"):return "stopped",""
    if event=="PermissionRequest":return "waiting","approval"
    if event in ("StopFailure",):return "idle","review failure"
    if event=="Interrupt":return "idle",""
    if event=="ui_prompt_start" or event=="PreToolUse" and tool in ("askuserquestion","request_user_input","request_user_input_async","enterplanmode","exitplanmode"):return "waiting","answer"
    if event=="Notification":
        reason={"permission_prompt":"approval","elicitation_dialog":"answer"}.get(payload.get("notification_type"))
        return ("waiting",reason) if reason else ("idle","")
    if event in ("SessionStart","session_start","Stop","agent_settled"):return "idle",""
    return "working",""


def hook(provider,event,payload,owner,pane=""):
    pid,started=owner
    sid=identity("session",provider,pid,started)
    # A provider session ID preserves a run over process restarts; fallback is attempt identity.
    thread=payload.get("session_id")
    run_id=identity("run",provider,thread) if isinstance(thread,str) and thread else sid
    child=payload.get("agent_id")
    if child:
        # Do not invent child lifecycle from parent tool events.
        return
    value=base(sid,run_id,provider,"provider hook")
    with database() as db:
        row=db.execute("SELECT payload FROM sessions WHERE id=?",(sid,)).fetchone()
    if row:value.update(json.loads(row[0]))
    value.update(observedAt=now(),source="provider hook")
    if thread and not value.get("parentId"): value["runId"]=run_id
    value["activity"],value["attention"]=event_activity(event,payload)
    value["summary"]={"Stop":"Turn finished; ready for input","agent_settled":"Turn finished; ready for input","Interrupt":"Interrupted by operator","SessionEnd":"Session ended","PermissionRequest":"Waiting for tool approval"}.get(event,event)
    cwd=payload.get("cwd") or os.environ.get("PWD","")
    if cwd:value["project"]=Path(cwd).name[:160]
    if pane and os.environ.get("TMUX"):
        import agent_status
        try:
            name=agent_status.run(["tmux","display-message","-p","-t",pane,"#{session_name}"])
            value["attachment"]={"host":host_id(),"socket":os.environ["TMUX"].split(",")[0],"session":name,"pane":pane}
        except Exception:pass
    record(value,event,pid,started)


def collect_interactive():
    import agent_status
    table=agent_status.processes()
    observed=set()
    with database() as db:
        runners=[(json.loads(raw),pid) for raw,pid in db.execute("SELECT payload,pid FROM sessions WHERE json_extract(payload,'$.runner')='ouroboros' AND pid IS NOT NULL")]

    # Read configured tmux servers, never remote proxy panes.
    servers=config().get("sockets",[None])
    for server in servers:
        cmd=["tmux"]+(["-S",server] if server else [])
        try:
            lines=agent_status.run(cmd+["list-panes","-a","-F","#{pane_id}\t#{pane_pid}\t#{session_name}\t#{pane_current_path}\t#{@tmax-remote-host}\t#{socket_path}"]).splitlines()
        except Exception:continue
        for line in lines:
            pane,pid,name,cwd,proxy,sock=line.split("\t",5)
            if proxy:continue
            for child in agent_status.descendants(int(pid),table):
                entry=table.get(child)
                if not entry:continue
                provider=agent_status.agent_name(entry[2])
                if not provider:continue
                sid=identity("session",provider,child,entry[1]);observed.add(sid)
                with database() as db:row=db.execute("SELECT payload FROM sessions WHERE id=?",(sid,)).fetchone()
                value=json.loads(row[0]) if row else base(sid,sid,provider,"process observation")
                value.update(observedAt=now(),project=Path(cwd).name[:160],attachment={"host":host_id(),"socket":sock,"session":name,"pane":pane})
                if not row:
                    value.update(summary="Agent process discovered; lifecycle hooks not yet observed",activity="unknown")
                for runner,runner_pid in runners:
                    if runner_pid in table and child in agent_status.descendants(runner_pid,table):
                        value.update(parentId=runner["id"],runId=runner["runId"],runner="ouroboros",role=runner["role"])
                        break
                record(value,"discovered" if not row else "location",child,entry[1])
    # Hooked sessions outside tmux remain observable too. Missing processes become stopped,
    # but no successful task outcome is inferred from their disappearance.
    with database() as db:rows=db.execute("SELECT payload,pid,started FROM sessions WHERE pid IS NOT NULL").fetchall()
    for raw,pid,started in rows:
        value=json.loads(raw)
        if value["provider"]=="ouroboros" or value["id"] in observed:continue
        alive=pid in table and table[pid][1]==started
        if alive:value["observedAt"]=now();record(value,"heartbeat",pid,started)
        elif value["activity"]!="stopped":
            value.update(activity="stopped",attention="review exit",summary="Process ended without an observed session-end event",stopReason="Process disappeared; outcome unknown",observedAt=now())
            record(value,"process_exit",pid,started)


def collect_ouroboros():
    import agent_status
    table=agent_status.processes()
    for root_string in config().get("ouroboros",[]):
        root=Path(root_string).expanduser()
        # Explicit project roots only; no recursive home-directory/transcript scan.
        for status in root.glob(".ouroboros/runs/*/status.json"):
            try:
                data=json.loads(status.read_text());run_dir=status.parent
                pid=int((run_dir/"pid").read_text().strip())
                run_id=identity("ouroboros",str(run_dir.resolve()))
                alive=pid in table and "ouroboros" in table[pid][2]
                # Pin process birth identity; a restart becomes a distinct attempt.
                started=table[pid][1] if alive else "ended"
                with database() as db:prior=db.execute("SELECT payload,started FROM sessions WHERE json_extract(payload,'$.runId')=? AND json_extract(payload,'$.provider')='ouroboros' ORDER BY rowid DESC LIMIT 1",(run_id,)).fetchone()
                if not alive and prior:
                    value=json.loads(prior[0]);sid=value["id"]
                else:
                    sid=identity("ouroboros-attempt",str(run_dir.resolve()),pid,started)
                    value=base(sid,run_id,"ouroboros","runner status")
                    if prior:
                        previous=json.loads(prior[0])
                        if previous["id"]!=sid and previous["activity"]!="stopped":
                            previous.update(activity="stopped",attention="review exit",summary="Runner restarted; prior attempt ended",stopReason="Superseded by a new process",observedAt=now())
                            record(previous,"attempt_replaced")
                stage=str(data.get("state","unknown"));stopped=stage in ("stopped","killed") or not alive
                activity="stopped" if stopped else "retrying" if stage in ("sleep","backoff","waiting","limited") else "working"
                human=run_dir/"NEEDS_HUMAN.md"
                attention="runner requests help" if human.exists() else "review exit" if not alive and stage not in ("stopped","killed") else ""
                reason=str(data.get("stop_reason",data.get("why","")))[:500]
                actor_key="actor:"+str(run_dir)
                with database() as db:
                    last_actor=db.execute("SELECT value FROM meta WHERE key=?",(actor_key,)).fetchone()
                if not last_actor:
                    history=run_dir/"iterations.jsonl"
                    if history.exists():
                        with history.open("rb") as stream:
                            stream.seek(max(0,history.stat().st_size-262144))
                            tail=stream.read().decode("utf8",errors="replace").splitlines()
                        for line in reversed(tail):
                            try: actor=json.loads(line)
                            except ValueError: continue
                            if actor.get("step")!="actor":continue
                            last_actor=(json.dumps({"failed":bool(actor.get("error") or actor.get("timed_out") or actor.get("exit",0)),"iteration":actor.get("iteration")}),)
                            with database() as db:db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)",(actor_key,last_actor[0]))
                            break
                if stopped and last_actor and json.loads(last_actor[0]).get("failed"):
                    attention="review failure"
                value.update(runner="ouroboros",project=root.name[:160],activity=activity,attention=attention,summary=(stage+": "+reason)[:500],role=str(data.get("role",stage))[:40],observedAt=now(),iteration=int(data.get("iteration",0)),stopReason=reason if stopped else "",evidence=(str(status)+(" · accepted "+str(data["last_ok_tag"]) if data.get("last_ok_tag") else ""))[:500],usage=str(data.get("usage_compact", ""))[:240])
                # Preserve runner-local terminal identity when available.
                try:
                    lines=agent_status.run(["tmux","list-panes","-a","-F","#{pane_id}\t#{pane_pid}\t#{session_name}\t#{socket_path}\t#{@tmax-remote-host}"]).splitlines()
                    for line in lines:
                        pane,rootpid,name,sock,proxy=line.split("\t",4)
                        if not proxy and pid in agent_status.descendants(int(rootpid),table):value["attachment"]={"host":host_id(),"socket":sock,"session":name,"pane":pane};break
                except Exception:pass
                record(value,"runner_state",pid,started if alive else None)
                # Include iteration/review transitions without copying transcripts or human text.
                iterations=run_dir/"iterations.jsonl"
                if iterations.exists():
                    key="offset:"+str(iterations)
                    with database() as db:
                        row=db.execute("SELECT value FROM meta WHERE key=?",(key,)).fetchone();offset=int(row[0]) if row else 0
                    if iterations.stat().st_size<offset:offset=0
                    with iterations.open() as stream:
                        stream.seek(offset)
                        for _ in range(100):
                            before=stream.tell();line=stream.readline()
                            if not line or not line.endswith("\n"):stream.seek(before);break
                            try:e=json.loads(line)
                            except ValueError:continue
                            step=str(e.get("step","iteration"))[:80]
                            detail=str(e.get("verdict",e.get("sha","")))
                            if step=="actor":
                                failed=bool(e.get("error") or e.get("timed_out") or e.get("exit",0))
                                detail="exit "+str(e.get("exit","unknown"))+(" · reported failure" if failed else "")
                                with database() as db:
                                    db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(actor_key,json.dumps({"failed":failed,"iteration":e.get("iteration")})))
                                if stopped:value["attention"]="review failure" if failed else ("runner requests help" if human.exists() else "")
                            ev=dict(value,summary=("Iteration "+str(e.get("iteration","?"))+" · "+step+" · "+detail)[:500],source="runner event")
                            # Timeline event must not overwrite the current runner state.
                            with database() as db:
                                eid=identity("runner-event",str(iterations),before)
                                db.execute("INSERT OR IGNORE INTO events(id,payload) VALUES(?,?)",(eid,json.dumps({"kind":step,"session":dict(ev,observedAt=e.get("ts",now()))})))
                        offset=stream.tell()
                    with database() as db:db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(offset)))
                    # Re-publish current state after historical events, preserving sequence order.
                    if row is None or offset!=int(row[0]):record(value,"runner_snapshot",pid,started if alive else None,force=True)
            except (OSError,ValueError,KeyError):continue


def publish():
    cfg=config();endpoint=cfg.get("endpoint")
    if not endpoint:return
    if not endpoint.startswith("https://") and not endpoint.startswith("http://127.0.0.1:"):raise ValueError("HTTPS required")
    token=Path(cfg["tokenFile"]).expanduser().read_text().strip()
    with database() as db:
        rows=db.execute("SELECT seq,id,payload FROM events WHERE sent=0 ORDER BY seq LIMIT 200").fetchall()
        values=[json.loads(row[0]) for row in db.execute("SELECT payload FROM sessions ORDER BY rowid DESC LIMIT 500")]
    body={"events":[dict(json.loads(payload),seq=seq,id=eid) for seq,eid,payload in rows],"observations":[{"id":v["id"],"observedAt":v["observedAt"]} for v in values]}
    request=urllib.request.Request(endpoint,data=json.dumps(body).encode(),headers={"Content-Type":"application/json","Authorization":"Bearer "+token,"X-Lab-Host":host_id()})
    with urllib.request.urlopen(request,timeout=8) as response:reply=json.load(response)
    accepted=set(reply.get("accepted",[]))
    with database() as db:
        for _,eid,_ in rows:
            if eid in accepted:db.execute("UPDATE events SET sent=1 WHERE id=?",(eid,))
        # Keep the latest 10,000 delivered events locally; unsent events are never pruned.
        db.execute("DELETE FROM events WHERE sent=1 AND seq<(SELECT COALESCE(MAX(seq),0)-10000 FROM events)")
        db.execute("INSERT INTO meta VALUES('published',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(now(),))


def snapshot():
    with database() as db:return [json.loads(row[0]) for row in db.execute("SELECT payload FROM sessions ORDER BY rowid DESC")]


def context_by_session():
    try:
        cfg=config(); endpoint=cfg.get("endpoint","")
        # Optional metadata cache from Pi enriches terminal labels with assigned tasks.
        cache=state_dir()/"overview.json"
        data=json.loads(cache.read_text()) if cache.exists() else {}
        tasks={t["id"]:t["title"] for t in data.get("tasks",[])}
        runs={r["id"]:tasks.get(r.get("taskId"),"") for r in data.get("runs",[])}
        values={v["id"]:v for v in data.get("sessions",[])}
        values.update({v["id"]:v for v in snapshot()})
        values=list(values.values());result={}
        for value in values:
            a=value.get("attachment")
            if not a or value["activity"]=="stopped":continue
            text=runs.get(value["runId"],"") or value.get("project","")
            reason=value.get("attention","")
            if reason:text+=(" · "+reason)
            entries=result.setdefault(a["host"],{})
            old=entries.get(a["session"])
            if not old or reason or not old["attention"]:
                entries[a["session"]]={"text":text[:100],"attention":bool(reason),"observedAt":value["observedAt"]}
        return result
    except Exception:return {}


def refresh_overview():
    cfg=config();endpoint=cfg.get("endpoint","")
    if not endpoint:return
    url=endpoint.removesuffix("/ingest") if hasattr(str,"removesuffix") else endpoint[:-7]
    with urllib.request.urlopen(url,timeout=8) as response: data=json.load(response)
    target=state_dir()/"overview.json";tmp=target.with_suffix(".tmp");tmp.write_text(json.dumps(data));tmp.chmod(0o600);tmp.replace(target)


def cycle():
    collect_ouroboros();collect_interactive()
    try:publish();refresh_overview()
    except Exception as e:
        # Never put tokens, response bodies, prompts or command lines into logs.
        print("agent publication unavailable: "+type(e).__name__,file=sys.stderr)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("command",choices=["once","serve","snapshot","context"]);args=p.parse_args()
    os.umask(0o077)
    if args.command=="snapshot":print(json.dumps(snapshot()));return
    if args.command=="context":print(json.dumps(context_by_session()));return
    import fcntl
    state_dir().mkdir(parents=True,exist_ok=True,mode=0o700)
    with (state_dir()/"reporter.lock").open("a") as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        while True:
            try:cycle()
            except Exception as e:print("agent observation unavailable: "+type(e).__name__,file=sys.stderr)
            if args.command=="once":return
            time.sleep(15)

if __name__=="__main__":main()
