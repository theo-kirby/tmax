#!/usr/bin/env python3
"""Install a configured metadata reporter. Does not configure SSH or agent permissions."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config",required=True,type=Path)
    p.add_argument("--enable",action="store_true")
    args=p.parse_args();cfg=json.loads(args.config.read_text())
    if not cfg.get("host") or not cfg.get("endpoint") or not Path(cfg.get("tokenFile","")).expanduser().is_file():raise SystemExit("Host, endpoint and existing private tokenFile required")
    os.umask(0o077)
    dest=Path.home()/".local/share/tmax";dest.mkdir(parents=True,exist_ok=True)
    for name in ("agent_status.py","lab_agents.py"):
        shutil.copy2(Path(__file__).parent/name,dest/name)
    target=Path.home()/".config/tmax/agents.json";target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(cfg,indent=2)+"\n");target.chmod(0o600)
    logs=Path.home()/".local/state/tmax-agents";logs.mkdir(parents=True,exist_ok=True)
    executable=sys.executable
    if sys.platform=="darwin":
        unit=Path.home()/"Library/LaunchAgents/dev.theo.tmax-agents.plist";unit.parent.mkdir(parents=True,exist_ok=True)
        payload={"Label":"dev.theo.tmax-agents","ProgramArguments":[executable,str(dest/"lab_agents.py"),"serve"],"RunAtLoad":True,"KeepAlive":True,"ThrottleInterval":30,"EnvironmentVariables":{"PATH":"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},"StandardOutPath":str(logs/"service.log"),"StandardErrorPath":str(logs/"service.log")}
        unit.write_bytes(plistlib.dumps(payload))
        if args.enable:
            subprocess.run(["launchctl","bootout",f"gui/{os.getuid()}/dev.theo.tmax-agents"],capture_output=True)
            subprocess.run(["launchctl","bootstrap",f"gui/{os.getuid()}",str(unit)],check=True)
    else:
        unit=Path.home()/".config/systemd/user/tmax-agents.service";unit.parent.mkdir(parents=True,exist_ok=True)
        unit.write_text('[Unit]\nDescription=tmax agent metadata reporter\n[Service]\nExecStart="'+executable+'" "'+str(dest/'lab_agents.py')+'" serve\nRestart=on-failure\nRestartSec=15\nUMask=0077\nEnvironment=PATH='+str(Path.home()/'.local/bin')+':/usr/local/bin:/usr/bin:/bin\n[Install]\nWantedBy=default.target\n')
        if args.enable:
            subprocess.run(["systemctl","--user","daemon-reload"],check=True)
            subprocess.run(["systemctl","--user","enable","--now","tmax-agents"],check=True)
    print("Reporter installed"+(" and enabled" if args.enable else ""))
if __name__=="__main__":main()
