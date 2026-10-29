"""The fork's own serving patches must survive every upstream merge.

Each test pins one thing this fork changes on top of upstream Strata, so a merge
that silently drops or overwrites a fork patch turns this file red:

- docker/entrypoint.sh h2 (9be2af85, 99fbffe0): serve keyless (--no-api-key) unless
  API_KEY/API_KEY_FILE gives a key, and no false "made one" warning; EXTRA_ENGINE_ARGS
  rewritten into `--set key=value` settings. EXTRA_CONFIG_JSON is unmentioned by design:
  a stale line in compose is simply unused (Simo 2026-10-24: the added hard stop was unnecessary).
- the /switch active_model override (entrypoint "fork patch" block): a container
  restart comes up on the model the last /switch chose.
- PARALLEL reaches `launch --parallel` (the compose speed contract: batch slots eat
  the expert-cache VRAM on the 4090, PARALLEL=1 is mandatory).
- serve/lifeline.py (ed33acfc): the portable watcher's token is the start time only.
- setup.py (1238dae9): every abliterated-family file is pinned in REPO_PINS.
- Dockerfiles + docker.yml: STRATA_COMMIT pass-through and the FORK_PAT basic-auth
  sync form (ce010c08, dcba1599, 5a5a49db, dbc48dff).
- serve/server.py: $STRATA_ALLOWED_HOSTS stays the keyless-LAN contract the compose relies on.
- serve/server.py /switch 0.2.x (ed753ecd+): the switch sees the 0.2.x layout - run settings
  in any candidate dir, model folders under <data>/Strata-data/models, and active_model
  written where the entrypoint reads it first ($STRATA_INSTALL_DIR).

Functional entrypoint tests rewrite the hardcoded absolute roots into a sandbox and
run the script against a stub `strata` that records its argv.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"
SH = shutil.which("sh")


def sh_path(p) -> str:
    cyg = shutil.which("cygpath")
    return str(p) if cyg is None else subprocess.run([cyg, "-u", str(p)], capture_output=True, text=True, check=True).stdout.strip()


STUB = """#!/bin/sh
printf '%s\\n' "ARGS: $*" >> "$STUB_LOG"
case "$1 $2" in
  "components engine")
    if [ "$3" = "--output" ]; then printf '{"usable": true, "backend": "cpu"}\\n'; fi ;;
  "launch --list-presets")
    printf '[]\\n' ;;
  "launch --status")
    printf '{"state": "ready"}\\n' ;;
  "launch --show-key") ;;
  "launch --stop") ;;
esac
if [ "$1 $2" = "launch --no-tui" ]; then printf '%s\\n' "KEY:${STRATA_LAUNCH_KEY:-none}" >> "$STUB_LOG"; fi
exit 0
"""


class EntrypointSandbox(unittest.TestCase):
    """Runs docker/entrypoint.sh with its /opt/strata, /data and /run roots rewritten into a temp dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.log = self.tmp / "stub.log"
        app = self.tmp / "opt" / "strata"
        (app / "terminal" / "bin").mkdir(parents=True)
        for d in ("data/install", "data/state", "run"):
            (self.tmp / d).mkdir(parents=True, exist_ok=True)
        stub = app / "terminal" / "bin" / "strata"
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)
        text = ENTRYPOINT.read_text(encoding="utf-8")
        app = self.tmp / "opt" / "strata"
        td = str(self.tmp / "data")
        for old, new in (
            ("APP=/opt/strata", f"APP={app}"),
            ("PHASE_FILE=/run/strata-phase", f"PHASE_FILE={self.tmp}/run/strata-phase"),
            ("mkdir -p /data/state /data/home /data/install /data/Strata-data /run",
             f"mkdir -p {td}/state {td}/home {td}/install {td}/Strata-data {self.tmp}/run"),
            ('"/data/', f'"{td}/'),          # every quoted absolute path
            ('" /data/', f'" {td}/'),         # the mountinfo patterns inside quotes
            ("in /data/install/", f"in {td}/install/"),
            ("--data-dir /data/Strata-data", f"--data-dir {td}/Strata-data"),
        ):
            text = text.replace(old, new)
        self.script = self.tmp / "entrypoint.sh"
        self.script.write_text(text, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def run_entry(self, **env):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("MODEL", "FAMILY", "VISION", "API_KEY", "EXTRA_", "PARALLEL", "TUNE", "STRATA_", "ACTIVE_", "XDG_", "GGUF", "KV", "GPU", "PORT", "PRESET"))}
        pydir = os.path.dirname(sys.executable)
        e.update({"STUB_LOG": str(self.log), "PATH": f"/usr/bin:/bin:{pydir}", "PRESET": "docker", "PORT": "8000",
                  "XDG_STATE_HOME": str(self.tmp / "data" / "state"),
                  "STRATA_KEY_FILE": str(self.tmp / "api-key"),
                  "STRATA_INSTALL_DIR": str(self.tmp / "data" / "install")})
        e.update({k: str(v) for k, v in env.items()})
        p = subprocess.Popen([SH, sh_path(self.script)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, env=e, text=True)
        # the sandboxed entry point stays up in its watch loop once "serving": stop it after that
        out_lines = []
        deadline = time.time() + 20
        while time.time() < deadline:
            if p.poll() is not None:
                break
            line = p.stdout.readline()
            if not line:
                break
            out_lines.append(line)
            if "Serving on port" in line:
                p.terminate()
                time.sleep(0.3)
                if p.poll() is None:
                    p.kill()
                break
        try:
            rest = p.communicate(timeout=5)
            out_lines.append(rest[0] or "")
            rc = p.returncode
        except subprocess.TimeoutExpired:
            p.kill()
            rc = p.returncode
        return rc, "".join(out_lines)

    def launch_args(self):
        if not self.log.exists():
            return []
        for line in self.log.read_text(encoding="utf-8").splitlines():
            if line.startswith("ARGS: launch --no-tui"):
                return shlex.split(line[len("ARGS: "):])
        return []

    def stub_key(self):
        if not self.log.exists():
            return None
        for line in self.log.read_text(encoding="utf-8").splitlines():
            if line.startswith("KEY:"):
                return line[len("KEY:"):]
        return None


@unittest.skipIf(SH is None, "needs a POSIX sh")
class TestEntrypointForkPatches(EntrypointSandbox):

    def test_serves_keyless_by_default_fork_patch(self):
        """9be2af85: no API_KEY in the environment -> --no-api-key, and the launch stays lan-accessible."""
        self.run_entry()
        args = self.launch_args()
        self.assertTrue(args, "the sandboxed entry point never reached `strata launch`")
        self.assertIn("--no-api-key", args, "fork patch lost: the 0.2.x default invents an API key again")
        self.assertNotIn("--api-key-env", args)
        self.assertIn("--access", args)
        self.assertEqual(args[args.index("--access") + 1], "lan")

    def test_no_false_made_one_warning_fork_patch(self):
        """99fbffe0: with --no-api-key no key is made, so the old warning must stay out."""
        rc, out = self.run_entry()
        self.assertNotIn("Strata made one", out)
        self.assertNotIn("made one", out)

    def test_api_key_env_bridge(self):
        """A set API_KEY still works: it travels as --api-key-env STRATA_LAUNCH_KEY, value in the launch env."""
        self.run_entry(API_KEY="0123456789abcdef")
        args = self.launch_args()
        self.assertIn("--api-key-env", args)
        self.assertEqual(args[args.index("--api-key-env") + 1], "STRATA_LAUNCH_KEY")
        self.assertNotIn("--no-api-key", args)
        self.assertEqual(self.stub_key(), "0123456789abcdef")

    def test_extra_engine_args_become_set_settings_fork_patch(self):
        """9be2af85: the old compose EXTRA_ENGINE_ARGS string -> `--set key=value`, bare flags become =on."""
        rc, out = self.run_entry(**{"EXTRA_ENGINE_ARGS": "--prefill auto:32768 --conversation-cache-mib 4096 --conversation-cache-slots 2 --spec 4 --kv-flag"})
        args = self.launch_args()
        sets = set()
        i = 0
        while i < len(args):
            if args[i] == "--set":
                sets.add(args[i + 1])
                i += 2
            else:
                i += 1
        for want in ("prefill=auto:32768", "conversation-cache-mib=4096", "conversation-cache-slots=2", "spec=4", "kv-flag=on"):
            self.assertIn(want, sets, "EXTRA_ENGINE_ARGS bridge lost or broke one setting")
        self.assertIn("EXTRA_ENGINE_ARGS applied as --set settings (fork patch)", out)

    def test_parallel_env_reaches_launch(self):
        """The compose speed contract: PARALLEL=1 -> `--parallel 1` (batch slots eat the expert-cache VRAM)."""
        self.run_entry(PARALLEL=1, MODEL="IQ3_S", FAMILY="abliterated")
        args = self.launch_args()
        self.assertIn("--parallel", args, "PARALLEL no longer reaches launch: the auto-fit can silently steal the expert cache again")
        self.assertEqual(args[args.index("--parallel") + 1], "1")
        self.assertEqual(args[args.index("--model") + 1], "IQ3_S")
        self.assertEqual(args[args.index("--family") + 1], "abliterated")

    def test_vision_cpu_honored_and_bogus_refused(self):
        self.run_entry(VISION="cpu")
        args = self.launch_args()
        self.assertEqual(args[args.index("--vision") + 1], "cpu")
        rc, out = self.run_entry(VISION="banana")
        self.assertEqual(rc, 2)
        self.assertIn("VISION=banana", out)

    def test_active_model_file_overrides_env_fork_patch(self):
        """The /switch fork patch: the file the server's /switch wrote decides the model at restart."""
        (self.tmp / "data" / "install" / "active_model").write_text("abliterated IQ3_S\n", encoding="utf-8")
        rc, out = self.run_entry(MODEL="Q2_0", FAMILY="qwen")
        self.assertIn("active_model: abliterated/IQ3_S overrides", out)
        args = self.launch_args()
        self.assertEqual(args[args.index("--model") + 1], "IQ3_S")
        self.assertEqual(args[args.index("--family") + 1], "abliterated")


class TestForkContractsOutsideTheEntrypoint(unittest.TestCase):

    def test_abliterated_family_fully_pinned_in_setup(self):
        """1238dae9: upstream's REPO_PINS table needs every downloaded file pinned; the abliterated line must survive merges."""
        tree = ast.parse((ROOT / "setup.py").read_text(encoding="utf-8"))
        pinned = [s.value for s in ast.walk(tree) if isinstance(s, ast.Constant) and isinstance(s.value, str)]
        shards = [p for p in pinned if "abliterated" in p and p.endswith(".gguf")]
        self.assertGreaterEqual(len(shards), 8, "the abliterated family's shard pins are missing from setup.py")
        repos = [p for p in pinned if p.startswith("SC117/") and "abliterated" in p]
        self.assertTrue(repos, "the abliterated repo pin is gone")

    def test_lifeline_token_is_start_time_only(self):
        """ed33acfc: STAT is mutable mid-life; the watcher's token must be lstart only (plus a separate zombie peek)."""
        path = ROOT / "serve" / "lifeline.py"
        src = path.read_text(encoding="utf-8")
        self.assertNotIn("lstart=,stat=", src, "the lifeline token regressed to the mutable lstart+stat form")
        spec = importlib.util.spec_from_file_location("lifeline_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))          # lifeline does `from serve import winjob`
        try:
            spec.loader.exec_module(mod)
        except SystemExit:
            self.skipTest("module wants a TTY at import")
        if sys.platform == "win32":
            self.skipTest("ps-based token on POSIX only")
        token = mod._start_token(os.getpid())
        ps = subprocess.run(["ps", "-o", "lstart=", "-p", str(os.getpid())], capture_output=True, text=True).stdout.strip()
        # Either start-time source is the contract: `ps -o lstart=` ("s:", what the fork wrote
        # until 2026-10-29) or procfs field 22 ("l:", the raw starttime the same ps formats).
        # What must never come back is STAT - the flake ed33acfc pinned.
        field22 = open(f"/proc/{os.getpid()}/stat", encoding="utf-8").read()
        field22 = field22[field22.rindex(")") + 2:].split()[19]
        self.assertIn(token, ("s:" + ps, "l:" + field22),
                        "the token is not exactly the process start time")

    def test_allowed_hosts_env_stays_the_keyless_contract(self):
        """STRATA_ALLOWED_HOSTS is why the keyless compose block works on the LAN; the name is a deployment contract."""
        src = (ROOT / "serve" / "server.py").read_text(encoding="utf-8")
        self.assertIn("STRATA_ALLOWED_HOSTS", src, "the env name the compose relies on changed")

    def test_dockerfiles_pass_the_source_commit(self):
        """ce010c08/dbc48dff/ae01fa14: the terminal stage has no .git; STRATA_COMMIT must flow in."""
        for f in (ROOT / "Dockerfile", ROOT / "docker" / "Dockerfile"):
            if not f.exists():
                continue
            text = f.read_text(encoding="utf-8")
            if "terminal" not in text.lower():
                continue
            self.assertIn("ARG STRATA_COMMIT", text, f"{f.name}: the commit pass-through was lost in a merge")
            self.assertIn("${STRATA_COMMIT}", text, f"{f.name}: STRATA_COMMIT declared but never used")

    def test_docker_workflow_syncs_with_fork_pat_basic_form(self):
        """5a5a49db/dcba1599: the sync push uses FORK_PAT in basic x-access-token form; the build gets github.sha."""
        text = (ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")
        self.assertIn("FORK_PAT", text, "the sync step lost FORK_PAT (GITHUB_TOKEN cannot write workflow files)")
        self.assertIn("x-access-token:", text, "the sync lost the proven basic-auth form")
        self.assertIn("STRATA_COMMIT=${{ github.sha }}", text,
                      "the build no longer receives the source commit")


    def test_switch_post_exits_when_idle_not_mid_request(self):
        """5ce26904: /switch must wait for the request FIFO; exiting mid-request killed the caller's own generation."""
        src = (ROOT / "serve" / "server.py").read_text(encoding="utf-8")
        self.assertIn("def _exit_when_idle(", src, "/switch exits mid-request again")
        self.assertIn("svc.fifo.acquire", src, "the idle wait no longer takes the request FIFO")

    def test_switch_get_lists_installed_models(self):
        """19d886ad: GET /switch answers with the switchable models (the tooling reads it)."""
        src = (ROOT / "serve" / "server.py").read_text(encoding="utf-8")
        self.assertIn('elif path == "/switch":', src, "GET /switch (the model list) vanished")
        self.assertIn("the switchable models", src, "GET /switch lost its list semantics")

    def test_switch_installed_unions_configs_and_model_folders(self):
        """0.2.x layout: GET /switch must list run settings from any candidate dir AND model
        folders that hold ggufs under <data>/Strata-data/models - the pre-0.2.x helper saw only
        one config dir, which is why a stock model already on disk answered 'installed: none'."""
        tree = ast.parse((ROOT / "serve" / "server.py").read_text(encoding="utf-8"))
        funcs = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in ("_switch_dirs", "_switch_config_dir", "_switch_installed"):
                node.decorator_list = []
                exec(compile(ast.Module(body=[node], type_ignores=[]), "<switch>", "exec"), {"Path": Path, "os": os}, funcs)

        class Svc:
            config_path = None

        class Self:
            _switch_dirs = funcs["_switch_dirs"]
            _switch_config_dir = funcs["_switch_config_dir"]
            _switch_installed = funcs["_switch_installed"]

        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            (data / "config").mkdir()
            (data / "config" / "strata-abliterated-iq3_s.json").write_text("{}", encoding="utf-8")
            folder = data / "Strata-data" / "models" / "qwen-IQ3_S"
            folder.mkdir(parents=True)
            (folder / "model.gguf").write_text("g", encoding="utf-8")
            (folder / "note.txt").write_text("x", encoding="utf-8")
            (data / "install").mkdir()
            os.environ["STRATA_DATA"], os.environ["STRATA_INSTALL_DIR"] = str(data), str(data / "install")
            try:
                names = Self()._switch_installed(Svc())
                wdir = Self()._switch_config_dir(Svc())
            finally:
                os.environ.pop("STRATA_DATA"), os.environ.pop("STRATA_INSTALL_DIR")
        self.assertIn("abliterated-iq3_s", names, "run settings from the old config dir stopped being listed")
        self.assertIn("qwen-IQ3_S", names, "model folders with ggufs stopped being listed")
        self.assertNotIn("note.txt", names, "non-model folders leak into the switch list")
        self.assertEqual(wdir, data / "install", "active_model must land where the entrypoint reads it first")

    def test_switch_writes_to_the_install_dir_even_with_a_run_config(self):
        """A live-found trap: the run config's dir is a fine place to LIST settings from but the
        entrypoint never reads active_model from it - a switch written there is reported by GET
        yet the restart comes up on the compose model. Writing must follow the entrypoint."""
        tree = ast.parse((ROOT / "serve" / "server.py").read_text(encoding="utf-8"))
        funcs = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in ("_switch_dirs", "_switch_config_dir"):
                node.decorator_list = []
                exec(compile(ast.Module(body=[node], type_ignores=[]), "<switch>", "exec"), {"Path": Path, "os": os}, funcs)

        class Self:
            _switch_dirs = funcs["_switch_dirs"]
            _switch_config_dir = funcs["_switch_config_dir"]

        with tempfile.TemporaryDirectory() as td:
            data, install = Path(td) / "data", Path(td) / "install"
            cfg = data / "Strata-data" / "config"
            cfg.mkdir(parents=True)
            install.mkdir()
            (cfg / "strata-iq3_s.json").write_text("{}", encoding="utf-8")

            class Svc:  # the real serving state: a run config exists inside the data volume
                config_path = str(cfg / "strata-iq3_s.json")

            os.environ["STRATA_DATA"], os.environ["STRATA_INSTALL_DIR"] = str(data), str(install)
            try:
                wdir = Self()._switch_config_dir(Svc())
                order = Self()._switch_dirs(Svc())
            finally:
                os.environ.pop("STRATA_DATA"), os.environ.pop("STRATA_INSTALL_DIR")
        self.assertEqual(wdir, install, "active_model written where the entrypoint never looks")
        self.assertEqual(order[:2], [install, data / "config"], "active_model read order must match the entrypoint")

    def test_switch_post_accepts_a_model_folder_without_a_run_setting(self):
        """POST /switch: a folder under Strata-data/models is switchable as it stands (launch
        re-makes its setting from the files; nothing downloads) - not only models that already
        have a strata-*.json."""
        src = (ROOT / "serve" / "server.py").read_text(encoding="utf-8")
        self.assertIn('for rel in (Path("Strata-data") / "models", Path("models")):', src,
                      "POST /switch stopped accepting 0.2.x model folders")
        self.assertIn('p.name.lower() in folder_names', src, "POST /switch folder match vanished")

    def test_switch_config_dir_goes_through_the_helper(self):
        """798255b8: /switch resolves the config dir through one helper (run-config symlink safe)."""
        src = (ROOT / "serve" / "server.py").read_text(encoding="utf-8")
        self.assertIn("def _switch_config_dir(", src, "the /switch paths stopped resolving through the helper")

    def test_restart_picks_one_deterministic_model_config(self):
        """88e31e96: an ambiguous config set once looped setup on 'Which one?' at EOF;
        today the choice is deterministic: active_model pins MODEL/FAMILY explicitly."""
        text = ENTRYPOINT.read_text(encoding="utf-8")
        self.assertIn('MODEL="$amod"; FAMILY="$afam"', text,
                      "the restart no longer pins one model config explicitly")
        self.assertIn("fork patch (/switch)", text, "the active_model override block vanished")

    def test_ci_merges_released_upstream_and_tags_from_releases(self):
        """c952ae1b + 728c12f9: only upstream RELEASES get merged; <upstream>-h<N> numbers come from releases/latest."""
        text = (ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")
        self.assertIn("releases/latest", text, "the release-only sync/tag version source was lost")
        self.assertIn("could not read upstream release version", text, "the tag version read lost its guard")

    def test_ci_builds_are_serialized(self):
        """dbcd7dff: two concurrent builds raced and reused the same -h<N> tag."""
        text = (ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")
        self.assertIn("group: strata-build-push", text, "builds are no longer serialized (tag races return)")

    def test_vision_allowed_for_ud_q4_pack(self):
        """b28bab56 (#967): images must stay allowed with the UD-Q4_K_XL pack."""
        s = (ROOT / "setup.py").read_text(encoding="utf-8")
        i = s.find('"UD-Q4_K_XL": {')
        self.assertGreater(i, -1, "the UD-Q4_K_XL pack entry vanished from setup.py")
        self.assertIn('"vision": True', s[i:i + 1500], "the #967 vision allowance regressed")

    @staticmethod
    def _extract_switch_resolve():
        tree = ast.parse((ROOT / "serve" / "server.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_switch_resolve":
                node.decorator_list = []
                ns: dict = {"Path": Path, "os": os}
                exec(compile(ast.Module(body=[node], type_ignores=[]), "<switch>", "exec"), ns, ns)
                return node.name, ns
        raise AssertionError("_switch_resolve vanished - POST /switch stopped validating targets")

    def test_switch_resolve_validates_targets_before_writing_active_model(self):
        """2026-10-29 live incident: POST /switch wrote a blind "qwen abliterated-IQ3_S", every
        later boot died with 'There is no model called abliterated-IQ3_S', and the container
        restart-looped 12 times before a human reset active_model. The POST must resolve the
        target through the launcher's own catalog first: valid targets come back canonical, a
        family tag written as one word resolves to the family that owns it, and unknown names
        are refused BEFORE the write."""
        name, ns = self._extract_switch_resolve()
        resolve = ns[name]

        class FakeCat:
            def resolve(self, model, family=None):
                if family is not None and family not in ("qwen", "abliterated"):
                    raise ValueError(f"There is no model family called '{family}'.")
                if model.upper() == "IQ3_S" and family in (None, "qwen", "abliterated"):
                    return (family or "qwen"), "IQ3_S"
                raise ValueError("There is no model called '" + model + "'. Models: IQ3_S. Families: qwen, abliterated.")

        class Self:
            _switch_resolve = resolve

            def __init__(self, cat):
                self._cat = cat

            def _switch_catalog(self):
                return self._cat

        s = Self(FakeCat())
        self.assertEqual(s._switch_resolve("qwen", "IQ3_S"), ("qwen", "IQ3_S", ""))
        self.assertEqual(s._switch_resolve("abliterated", "IQ3_S"), ("abliterated", "IQ3_S", ""))
        self.assertEqual(s._switch_resolve("qwen", "abliterated-IQ3_S"), ("abliterated", "IQ3_S", ""),
                         "the one-word family tag the switch list shows must resolve, not crash-loop")
        fam, mod, err = s._switch_resolve("qwen", "definitely-not-a-model")
        self.assertIsNone(fam, "an unknown model slipped through the resolver")
        self.assertIn("definitely-not-a-model", err)
        self.assertEqual(Self(None)._switch_resolve("anyone", "anything"), ("anyone", "anything", ""),
                         "no catalog must not block switching")

    def test_switch_post_orders_resolve_before_the_active_model_write(self):
        src = (ROOT / "serve" / "server.py").read_text(encoding="utf-8")
        self.assertIn("self._switch_resolve(", src, "POST /switch stopped validating the target")
        self.assertLess(src.index("self._switch_resolve("), src.index('write_text(f"{fam} {mod}'),
                        "active_model can be written again before the catalog has vetted the target")

    def test_switch_resolve_agrees_with_the_real_catalog(self):
        """Against the real setup.py tables: the incident payload must resolve, and a name the
        boot would reject must be refused with the boot's own message."""
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        try:
            from strata_cli.launch.catalog import Catalog
            cat = Catalog()
        except Exception:
            self.skipTest("this checkout carries no installer files for the catalog")
        name, ns = self._extract_switch_resolve()

        class Self:
            _switch_resolve = ns[name]

            def _switch_catalog(self):
                return cat

        self.assertEqual(Self()._switch_resolve("qwen", "IQ3_S"), ("qwen", "IQ3_S", ""))
        self.assertEqual(Self()._switch_resolve("qwen", "abliterated-IQ3_S"), ("abliterated", "IQ3_S", ""),
                         "the exact 2026-10-29 payload must land on the abliterated family")
        fam, mod, err = Self()._switch_resolve("qwen", "definitely-not-a-model")
        self.assertIsNone(fam)
        self.assertIn("There is no model called", err, "the 404 must carry the boot's own wording")


if __name__ == "__main__":
    unittest.main()
