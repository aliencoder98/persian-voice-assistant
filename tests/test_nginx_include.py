import importlib.util
import pathlib

import pytest

SPEC = importlib.util.spec_from_file_location(
    "nginx_include", pathlib.Path(__file__).resolve().parent.parent / "scripts" / "nginx_include.py"
)
ni = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ni)

INCLUDE = "snippets/assistant-dashboard.conf"

SITE = """\
# main site
server {
    listen 80;
    listen [::]:80;
    server_name didrah.com www.didrah.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name didrah.com www.didrah.com;   # server_name tg.example.com;
    ssl_certificate /etc/letsencrypt/live/didrah.com/fullchain.pem;

    root /var/www/didrah;
    location / {
        if ($request_uri ~ "^/old") { return 301 /new; }
        try_files $uri $uri/ /index.html;
    }
}
"""

OTHER = """\
server {
    listen 443 ssl;
    server_name other.example.org;
    location / { return 200; }
}
"""


def run(tmp_path, files, domain="didrah.com"):
    d = tmp_path / "sites"
    d.mkdir(exist_ok=True)
    for name, text in files.items():
        (d / name).write_text(text, encoding="utf-8")
    return ni.main(["--domain", domain, "--include", INCLUDE, "--dirs", str(d)]), d


def test_include_goes_into_the_https_block_only(tmp_path):
    rc, d = run(tmp_path, {"didrah": SITE, "other": OTHER})
    assert rc == 0
    text = (d / "didrah").read_text(encoding="utf-8")
    assert text.count(f"include {INCLUDE};") == 1
    https_block = text[text.index("listen 443") :]
    assert f"include {INCLUDE};" in https_block
    assert f"include {INCLUDE};" not in text[: text.index("listen 443")]  # not in the port-80 redirect block
    assert text.count("{") == text.count("}")
    assert "try_files $uri $uri/ /index.html;" in text and "ssl_certificate" in text
    assert (d / "didrah.bak-assistant").read_text(encoding="utf-8") == SITE
    assert (d / "other").read_text(encoding="utf-8") == OTHER  # untouched


def test_include_is_the_last_thing_in_the_block(tmp_path):
    rc, d = run(tmp_path, {"didrah": SITE})
    text = (d / "didrah").read_text(encoding="utf-8")
    assert text.rstrip().endswith("}") and text.rstrip().splitlines()[-2].strip().startswith("include")


def test_running_twice_changes_nothing_the_second_time(tmp_path):
    run(tmp_path, {"didrah": SITE})
    d = tmp_path / "sites"
    after_first = (d / "didrah").read_text(encoding="utf-8")
    rc, _ = run(tmp_path, {}) if False else (ni.main(["--domain", "didrah.com", "--include", INCLUDE, "--dirs", str(d)]), d)
    assert rc == 0 and (d / "didrah").read_text(encoding="utf-8") == after_first


def test_a_subdomain_block_is_not_mistaken_for_the_domain(tmp_path):
    only_tg = OTHER.replace("other.example.org", "tg.didrah.com")
    rc, d = run(tmp_path, {"tg": only_tg})
    assert rc == 2 and (d / "tg").read_text(encoding="utf-8") == only_tg


def test_comments_do_not_count(tmp_path):
    commented = "# server { listen 443; server_name didrah.com; }\nserver { listen 80; server_name example.net; }\n"
    rc, _ = run(tmp_path, {"c": commented})
    assert rc == 2


def test_http_only_block_is_not_enough(tmp_path):
    rc, _ = run(tmp_path, {"p": "server {\n listen 80;\n server_name didrah.com;\n}\n"})
    assert rc == 2


def test_two_matching_files_are_left_alone(tmp_path):
    rc, d = run(tmp_path, {"a": SITE, "b": SITE})
    assert rc == 3
    assert (d / "a").read_text(encoding="utf-8") == SITE and not list(d.glob("*.bak-assistant"))


def test_no_files_found(tmp_path):
    assert ni.main(["--domain", "didrah.com", "--include", INCLUDE, "--dirs", str(tmp_path / "missing")]) == 2


@pytest.mark.parametrize("listen", ["listen 443 ssl;", "listen [::]:443 ssl http2;", "listen 443;"])
def test_listen_variants(tmp_path, listen):
    rc, d = run(tmp_path, {"v": f"server {{\n {listen}\n server_name didrah.com;\n}}\n"})
    assert rc == 0 and "include" in (d / "v").read_text(encoding="utf-8")
