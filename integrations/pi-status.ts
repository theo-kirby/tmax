// Installed as ~/.pi/agent/extensions/tmax-status.ts
import { execFile } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

export default function (pi: any) {
  let pending = Promise.resolve();
  const report = (event: string) => {
    if (!process.env.TMUX_PANE) return;
    pending = pending.then(() => new Promise<void>((resolve) => {
      execFile("python3", [join(homedir(), ".local/share/tmax/agent_status.py"), "hook", "pi", event],
      { env: { ...process.env, TMAX_AGENT_PID: String(process.pid) }, timeout: 4000 },
      () => resolve());
    }));
    return pending;
  };
  for (const event of ["session_start", "agent_start", "agent_settled",
                       "ui_prompt_start", "ui_prompt_end", "session_shutdown"]) {
    pi.on(event, () => report(event));
  }
}
