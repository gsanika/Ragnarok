import argparse
import json
import sys
import time

from .config import Config
from .llm import OllamaError


def _interactive_approval(skill: str, args: dict) -> bool:
    return input(f"\n  Allow '{skill}' with {json.dumps(args)[:200]}? [y/N] ").strip().lower() == "y"


def _runtime(interactive=False, role="assistant", offline=False):
    from .memory import HashEmbedder
    from .runtime import Runtime
    emb = HashEmbedder() if offline else None
    return Runtime(Config(), role=role, embedder=emb, interactive_approval=_interactive_approval if interactive else None)


def _doctor() -> int:
    import os
    import stat
    from .llm import OllamaClient
    from .sandbox import docker_available
    cfg, bad = Config(), 0
    def line(ok, msg, hint=""):
        nonlocal bad
        bad += 0 if ok else 1
        print(("  ok   " if ok else "  FAIL ") + msg + ("" if ok or not hint else f"\n         -> {hint}"))
    st = OllamaClient(cfg).status()
    line(st["reachable"], f"Ollama reachable at {cfg.ollama_url}", "run `ollama serve`")
    if st["reachable"]:
        line(st.get("chat_model_ok"), f"chat model {cfg.chat_model}", f"ollama pull {cfg.chat_model}")
        line(st.get("embed_model_ok"), f"embedding model {cfg.embed_model}", f"ollama pull {cfg.embed_model}")
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True); t = cfg.data_dir / ".w"; t.write_text("x"); t.unlink()
        line(True, f"data dir writable ({cfg.data_dir.resolve()})")
    except OSError as e:
        line(False, "data dir writable", str(e))
    kf = cfg.data_dir / "key.bin"
    if os.environ.get("RAGNAROK_PASSPHRASE"):
        line(True, "memory key derived from RAGNAROK_PASSPHRASE")
    elif kf.exists():
        line(not (kf.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)), "key file permissions are private", f"chmod 600 {kf}")
    else:
        print("  info key file will be created on first run (back it up, or set RAGNAROK_PASSPHRASE)")
    print("  " + ("ok   " if docker_available() else "info ") + "docker " + ("available: sandbox will use it (no network)" if docker_available() else "not available: sandbox uses process limits only"))
    try:
        import numpy  # noqa: F401
        print("  ok   numpy installed (fast recall)")
    except ImportError:
        print("  info numpy missing: `pip install numpy` for faster recall on large memories")
    print("\nAll good." if not bad else f"\n{bad} problem(s) found.")
    return 1 if bad else 0


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="ragnarok", description="Sovereign, always-on personal AI agent (local Ollama only).")
    p.add_argument("--role", default="assistant", choices=["viewer", "assistant", "admin"])
    p.add_argument("--offline-embeddings", action="store_true", help="use the built-in hash embedder instead of Ollama embeddings")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("chat", help="interactive chat (default)")
    d = sub.add_parser("do", help="plan and execute a multi-step goal"); d.add_argument("goal")
    sub.add_parser("brief", help="print the morning brief")
    dm = sub.add_parser("daemon", help="always-on mode: triggers, daily brief, memory maintenance")
    dm.add_argument("--ui", action="store_true", help="also serve the web UI"); dm.add_argument("--port", type=int, default=8765)
    dm.add_argument("--poll", type=float, default=5.0); dm.add_argument("--brief-at", default="08:00"); dm.add_argument("--maintenance-at", default="03:00")
    u = sub.add_parser("ui", help="local web UI (memory, approvals, chat, log)"); u.add_argument("--port", type=int, default=8765)
    m = sub.add_parser("memory", help="view / edit / export / delete memories")
    ms = m.add_subparsers(dest="mcmd", required=True)
    ms.add_parser("list"); a = ms.add_parser("add"); a.add_argument("text")
    e = ms.add_parser("edit"); e.add_argument("id", type=int); e.add_argument("text")
    x = ms.add_parser("delete"); x.add_argument("id", type=int)
    ms.add_parser("export"); ms.add_parser("consolidate"); ms.add_parser("forget")
    w = ms.add_parser("wipe"); w.add_argument("--yes", action="store_true")
    ap = sub.add_parser("approvals", help="pending risky-action approvals")
    aps = ap.add_subparsers(dest="acmd", required=True)
    aps.add_parser("list"); g = aps.add_parser("grant"); g.add_argument("id"); r = aps.add_parser("reject"); r.add_argument("id")
    sub.add_parser("doctor", help="check Ollama, models, key file, sandbox and optional extras")
    b = sub.add_parser("bench", help="benchmark the non-functional requirements"); b.add_argument("--ollama", action="store_true"); b.add_argument("--facts", type=int, default=2000)
    args = p.parse_args(argv)
    cmd = args.cmd or "chat"

    if cmd == "bench":
        from . import bench
        emb = None
        if args.ollama:
            from .llm import OllamaClient
            emb = OllamaClient()
        print(bench.report(bench.run(emb, n_facts=args.facts, out=Config().data_dir / "bench.json"))); return

    if cmd == "doctor":
        sys.exit(_doctor())

    rt = None
    try:
        rt = _runtime(interactive=cmd in ("chat", "do"), role=args.role, offline=args.offline_embeddings)
        if cmd == "chat":
            print(f"Ragnarok ready (model: {rt.config.chat_model}, role: {rt.policy.role}). Ctrl-D to quit.")
            try:
                while True:
                    t = input("you> ").strip()
                    if not t:
                        continue
                    try:
                        print("ragnarok>", rt.chat(t))
                    except OllamaError as e:      # keep the session alive; user can start Ollama and retry
                        print(f"ragnarok: {e}")
            except (EOFError, KeyboardInterrupt):
                print()
        elif cmd == "do":
            print(rt.do(args.goal))
        elif cmd == "brief":
            print(rt.brief())
        elif cmd == "daemon":
            rt.start_daemon(args.poll, args.brief_at, args.maintenance_at)
            print("Ragnarok daemon running. Ctrl-C to stop. Notifications: data/notifications.jsonl")
            if args.ui:
                from .server import serve
                serve(rt, args.port)
            else:
                try:
                    while True: time.sleep(3600)
                except KeyboardInterrupt:
                    pass
        elif cmd == "ui":
            from .server import serve
            serve(rt, args.port)
        elif cmd == "memory":
            mm = rt.memory
            if args.mcmd == "list":
                for f in rt.store.facts(): print(f"#{f.id} [{f.importance:.2f}, used {f.uses}x] {f.text}")
            elif args.mcmd == "add": print("id", mm.remember(args.text, 0.7, "user"))
            elif args.mcmd == "edit": mm.edit(args.id, args.text)
            elif args.mcmd == "delete": print("deleted" if mm.delete(args.id) else "not found")
            elif args.mcmd == "export": print(json.dumps(mm.export(), indent=2, ensure_ascii=False))
            elif args.mcmd == "consolidate": print("new facts:", mm.consolidate(rt.llm))
            elif args.mcmd == "forget": print(mm.forget())
            elif args.mcmd == "wipe":
                if args.yes or input("Type WIPE to erase all memory: ") == "WIPE":
                    mm.wipe(); print("memory erased")
        elif cmd == "approvals":
            if args.acmd == "list":
                for x in rt.approvals.pending(): print(x["id"], x["skill"], json.dumps(x["args"]))
            elif args.acmd == "grant": print(rt.approvals.grant(args.id))
            else: print(rt.approvals.reject(args.id))
    except OllamaError as e:
        print(f"\nragnarok: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if rt is not None:
            rt.close()


if __name__ == "__main__":
    main(sys.argv[1:])
