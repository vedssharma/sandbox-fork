# Reference fix used only by validate.py: keep the store across reloads, parse ids properly (legacy
# L- ids are offset), make the migration idempotent, reload the code, then migrate.
python3 - <<'PY'
p = "app/sessions.py"
s = open(p).read()
s = s.replace("STORE: dict = {}", "STORE: dict = globals().get(\"STORE\", {})  # survives importlib.reload")
s = s.replace("import time\n", "import time\n\nfrom app.users import parse_user_id\n")
s = s.replace('{"user": user,', '{"user": parse_user_id(user),')
open(p, "w").write(s)
p = "migrations/0004_int_user_ids.py"
s = open(p).read()
s = s.replace('session["user"] = int(session["user"].split("-")[1])',
              'from app.users import parse_user_id\n        session["user"] = parse_user_id(session["user"])')
open(p, "w").write(s)
PY
curl -sf -X POST http://127.0.0.1:8080/admin/reload -d '{"module": "app.users"}'
curl -sf -X POST http://127.0.0.1:8080/admin/reload -d '{"module": "app.sessions"}'
./migrate.sh 0004_int_user_ids
curl -s http://127.0.0.1:8080/stats
