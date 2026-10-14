"""Command line.

    python -m lotowriter serve [--host 0.0.0.0] [--port 8000]
    python -m lotowriter generate <procedure.json> [--out DIR] [--no-pdf]
    python -m lotowriter reset-admin          # admin password back to Password1 (must change at login)
"""
import argparse
import os
import sys


def cmd_generate(args):
    from .generate import generate, load_procedure
    result = generate(load_procedure(args.procedure), args.out, pdf=not args.no_pdf)
    print(f"Procedure {result.procedure_number}")
    print(f"  DOCX: {result.docx_path}")
    if result.pdf_path:
        print(f"  PDF:  {result.pdf_path}")
    if result.warnings:
        print("Completeness warnings:")
        for w in result.warnings:
            print(f"  - {w}")
    return 0


def cmd_serve(args):
    import uvicorn
    uvicorn.run("lotowriter.web.app:app", host=args.host, port=args.port, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1")
    return 0


def cmd_reset_admin(_args):
    from .auth import end_sessions, set_password
    from .db import DEFAULT_ADMIN_PASSWORD, connect, migrate
    conn = connect()
    migrate(conn)
    user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
    if user is None:
        print("No 'admin' account found.")
        return 1
    set_password(conn, user["id"], DEFAULT_ADMIN_PASSWORD, must_change=True)
    conn.execute("UPDATE users SET active = 1, role = 'admin' WHERE id = ?", (user["id"],))
    end_sessions(conn, user["id"])
    conn.execute("INSERT INTO audit_log(user_id, entity, entity_id, action) VALUES (NULL, 'user', ?, "
                 "'reset_admin_cli')", (user["id"],))
    print(f"'admin' password reset to {DEFAULT_ADMIN_PASSWORD}. Sign in and change it immediately.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lotowriter")
    sub = ap.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="generate an EHS-003a from a procedure JSON file")
    g.add_argument("procedure")
    g.add_argument("--out", default="out")
    g.add_argument("--no-pdf", action="store_true")
    g.set_defaults(func=cmd_generate)

    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default=os.environ.get("LOTOWRITER_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("LOTOWRITER_PORT", "8000")))
    s.set_defaults(func=cmd_serve)

    r = sub.add_parser("reset-admin", help="reset the admin password to the default")
    r.set_defaults(func=cmd_reset_admin)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
