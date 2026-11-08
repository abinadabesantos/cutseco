import datetime, json, os, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

GRAPH = "https://graph.facebook.com/v26.0/"
AGENDA = "agenda.json"
JANELA = 35
TENTATIVAS = 3
VIGIA = 320   # min por execução (o job tem limite de 360)

TOKEN, IG = os.environ["META_TOKEN"], os.environ["IG_ID"]
GH, REPO = os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"]


def agora():
    return datetime.datetime.now(datetime.timezone.utc)


def meta(method, path, **params):
    params["access_token"] = TOKEN
    url = GRAPH + path
    data = None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
    try:
        return json.load(urllib.request.urlopen(urllib.request.Request(url, data, method=method), timeout=60))
    except urllib.error.HTTPError as e:
        err = json.load(e).get("error", {})
        raise RuntimeError("%s (código %s)" % (err.get("error_user_msg") or err.get("message"), err.get("code")))


def gh(method, url):
    req = urllib.request.Request(url, method=method, headers={"Authorization": "Bearer " + GH,
                                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as f:
        b = f.read()
        return json.loads(b) if b else None


def apagar_asset(asset_id):
    if asset_id:
        try:
            gh("DELETE", "https://api.github.com/repos/%s/releases/assets/%s" % (REPO, asset_id))
        except urllib.error.HTTPError as e:
            print("  não apaguei o asset %s: %s" % (asset_id, e), flush=True)


def mudar(fn, msg):
    """Aplica fn na agenda mais recente do GitHub e grava. Nunca sobrescreve uma mudança feita de fora (o
    agenda_ig.py mexe na agenda pelo GitHub enquanto a vigia roda): em vez de rebase, busca, reaplica e tenta de novo.
    Em 08/11/2026 um rebase em conflito fez a vigia perder o "publicado" e o Reel saiu duas vezes."""
    for tentativa in range(6):
        subprocess.run(["git", "fetch", "-q", "origin", "main"], check=True)
        subprocess.run(["git", "reset", "-q", "--hard", "origin/main"], check=True)
        d = json.load(open(AGENDA, encoding="utf-8"))
        fn(d)
        json.dump(d, open(AGENDA, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        subprocess.run(["git", "add", AGENDA], check=True)
        if not subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode:
            return d
        subprocess.run(["git", "commit", "-qm", msg], check=True)
        if subprocess.run(["git", "push", "-q", "origin", "HEAD:main"]).returncode == 0:
            return d
        time.sleep(5)
    raise RuntimeError("não consegui gravar a agenda (%s)" % msg)


def no_post(pid, fn):
    def f(d):
        for p in d["posts"]:
            if p["id"] == pid:
                fn(p)
    return f


def atual(pid):
    subprocess.run(["git", "fetch", "-q", "origin", "main"], check=True)
    d = json.loads(subprocess.run(["git", "show", "origin/main:" + AGENDA], capture_output=True, check=True).stdout)
    return next((p for p in d["posts"] if p["id"] == pid), None)


def container(p):
    campos = dict(media_type="REELS", video_url=p["video_url"], caption=p["legenda"], share_to_feed="true")
    if p.get("capa_url"):
        campos["cover_url"] = p["capa_url"]
    cont = meta("POST", IG + "/media", **campos)["id"]
    for _ in range(120):
        st = meta("GET", cont, fields="status_code,status")
        if st.get("status_code") == "FINISHED":
            return cont
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError("container %s: %s" % (cont, st.get("status")))
        time.sleep(10)
    raise RuntimeError("container %s não terminou em 20 min" % cont)


def publicar(p):
    cont = container(p)
    falta = (datetime.datetime.fromisoformat(p["quando"]) - agora()).total_seconds()
    print("  container %s pronto, esperando %d s" % (cont, max(falta, 0)), flush=True)
    if falta > 0:
        time.sleep(falta)
    # confere de novo antes de publicar: o post pode ter sido cancelado ou mudado de horário enquanto esperava
    q = atual(p["id"])
    if not q or q.get("status") != "publicando" or q["quando"] != p["quando"]:
        print("  mudou na agenda enquanto esperava; não publico", flush=True)
        return None
    if p.get("ensaio"):
        return "ensaio ok: container %s FINISHED, horário %s; não publicado" % (cont, agora().isoformat(timespec="seconds"))
    for i in range(6):
        try:
            mid = meta("POST", IG + "/media_publish", creation_id=cont)["id"]
            break
        except RuntimeError as e:
            if i == 5:
                raise
            print("  publish: %s; tentando de novo" % e, flush=True)
            time.sleep(20)
    return meta("GET", mid, fields="permalink").get("permalink") or "publicado (id %s)" % mid


def ciclo():
    d = json.load(open(AGENDA, encoding="utf-8"))
    limite = agora() + datetime.timedelta(minutes=JANELA)
    fila = sorted((p for p in d["posts"] if p.get("status", "pendente") == "pendente"
                   and datetime.datetime.fromisoformat(p["quando"]) <= limite), key=lambda p: p["quando"])
    for p in fila:
        print(p["quando"], p["id"], flush=True)
        # sem limite de atraso (pedido de 08/11/2026): é melhor o Reel sair atrasado do que não sair.
        # "publicando" marca o post como da vigia; se a execução morrer no meio, ele fica assim (não repete sozinho)
        pego = {}
        def pegar(q):
            if q.get("status", "pendente") == "pendente" and q["quando"] == p["quando"]:
                q.update(status="publicando", pego_em=agora().isoformat(timespec="seconds")); pego["ok"] = True
        mudar(no_post(p["id"], pegar), "publicando " + p["id"])
        if not pego:
            print("  mudou na agenda; pulo", flush=True)
            continue
        try:
            link = publicar(p)
        except Exception as e:
            print("  falhou: %s" % e, flush=True)
            def falhou(q, e=e):
                q["tentativas"] = q.get("tentativas", 0) + 1
                q["erro"] = str(e)[:300]
                q["status"] = "erro" if q["tentativas"] >= TENTATIVAS else "pendente"
                q.pop("pego_em", None)
            mudar(no_post(p["id"], falhou), "agenda")
            continue
        if link is None:
            continue
        def feito(q, link=link):
            if q.get("status") == "publicando":
                enxuto = {"id": q["id"], "quando": q["quando"], "status": "ensaiado" if q.get("ensaio") else "publicado",
                          "permalink": link, "publicado_em": agora().isoformat(timespec="seconds")}
                q.clear(); q.update(enxuto)
        mudar(no_post(p["id"], feito), "agenda")
        print("  ok:", link, flush=True)
        for a in (p.get("video_asset"), p.get("capa_asset")):
            apagar_asset(a)
    return json.load(open(AGENDA, encoding="utf-8"))


def main():
    if os.environ.get("VERIFICAR") == "true":
        print("Token ok: @%s" % meta("GET", IG, fields="username")["username"])
        return
    # O cron do GitHub não roda de 15 em 15 min de verdade (em 07-08/11/2026 rodou a cada 3-7 h), então cada
    # execução fica de vigia por até VIGIA min, olhando a agenda a cada minuto, e no fim dispara a próxima
    # (workflow_dispatch pelo GITHUB_TOKEN inicia outra execução) enquanto houver post pendente.
    fim = agora() + datetime.timedelta(minutes=VIGIA)
    while True:
        subprocess.run(["git", "fetch", "-q", "origin", "main"])
        subprocess.run(["git", "reset", "-q", "--hard", "origin/main"])
        d = ciclo()
        pendentes = [p for p in d["posts"] if p.get("status", "pendente") == "pendente"]
        if not pendentes:
            print("Nada pendente; encerrando sem encadear", flush=True)
            return
        if agora() >= fim:
            break
        time.sleep(60)
    try:
        req = urllib.request.Request("https://api.github.com/repos/%s/actions/workflows/publicar.yml/dispatches" % REPO,
                                     json.dumps({"ref": "main"}).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + GH, "Accept": "application/vnd.github+json"})
        urllib.request.urlopen(req, timeout=60)
        print("Próxima vigia disparada", flush=True)
    except urllib.error.HTTPError as e:
        print("não consegui disparar a próxima vigia: %s %s" % (e, e.read()[:300]), flush=True)


if __name__ == "__main__":
    main()
