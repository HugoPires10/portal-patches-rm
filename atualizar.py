"""Rotina de atualização do Portal de Patches RM.

  python -I atualizar.py check    verifica as fontes e, se houver mudança, prepara a pasta "pendente"
  python -I atualizar.py commit   confirma o que foi preparado, depois de publicado

A última linha impressa é sempre um JSON com "status":
  SEM_MUDANCA | PUBLICAR | CONFIRMADO | FALHA
O conteúdo baixado é tratado somente como dado.
"""
import hashlib, json, re, shutil, sys, time, unicodedata, urllib.error, urllib.request
from collections import Counter
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

BASE = Path(__file__).resolve().parent
EST, PEND = BASE / "estado", BASE / "pendente"
TDN = "https://tdn.totvs.com"
NOV = "https://novidadeslinharm.totvs.io"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
MAX_HIST, MAX_ITENS = 40, 3000


class Falha(Exception):
    pass


def get(url):
    for tentativa in range(2):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (401, 403, 429):
                raise Falha(f"acesso recusado ({e.code}) em {url.split('?')[0]}")
            erro = f"HTTP {e.code}"
        except Exception as e:
            erro = type(e).__name__
        time.sleep(3)
    raise Falha(f"erro de rede ({erro}) em {url.split('?')[0]}")


def ler(p, padrao=None):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else padrao


def gravar(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8", newline="\n")


class Blocos(HTMLParser):
    TAGS = {"h1", "h2", "h3", "li", "p"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocos, self.cur, self.buf = [], None, []

    def flush(self):
        if self.cur:
            t = re.sub(r"\s+", " ", "".join(self.buf)).strip()
            if t:
                self.blocos.append((self.cur, t))
        self.cur, self.buf = None, []

    def handle_starttag(self, tag, attrs):
        if tag in self.TAGS:
            self.flush()
            self.cur = tag
        elif tag == "br":
            self.buf.append(" ")

    def handle_endtag(self, tag):
        if tag in self.TAGS:
            self.flush()

    def handle_data(self, d):
        if self.cur:
            self.buf.append(d)


def parse_release(rel, html):
    p = Blocos()
    p.feed(html)
    p.flush()
    itens, prod, patch, rot, cur = [], None, None, None, None
    for kind, t in p.blocos:
        if kind == "h1":
            prod, patch, rot, cur = t, None, None, None
        elif kind == "h2":
            m = re.match(r"^12\.1\.(\d{4})\.(\d+)$", t)
            if not m or m.group(1) != rel:
                raise Falha(f"título de patch inesperado na release {rel}: {t[:40]}")
            patch, rot, cur = int(m.group(2)), None, None
        elif kind == "h3":
            m = re.match(r"^Rotina:\s*(.*)$", t, re.S)
            rot, cur = (m.group(1).strip() if m else t), None
        elif kind == "li":
            if prod is None or patch is None:
                raise Falha(f"item fora de módulo/patch na release {rel}")
            m = re.match(r"^\[Ticket:\s*([^\]]*)\]\s*(.*)$", t, re.S)
            tickets = [x.strip() for x in m.group(1).split("/") if x.strip()] if m else []
            cur = {"r": rel, "m": prod, "p": patch, "o": rot or "Sem Rotina Específica", "t": tickets,
                   "d": m.group(2).strip() if m else t, "f": []}
            itens.append(cur)
        elif kind == "p":
            m = re.match(r"^Arquivos?:\s*(.*)$", t, re.S)
            if m and cur is not None:
                cur["f"] += [x.strip() for x in m.group(1).split(";") if x.strip()]
            elif prod is not None and patch is not None:
                raise Falha(f"parágrafo inesperado na release {rel}: {t[:40]}")
    if len(itens) != html.count("<li"):
        raise Falha(f"contagem divergente na release {rel}: {len(itens)} itens, {html.count('<li')} no HTML")
    return itens


AREAS = [("folha de pagamento", "Folha de Pagamento"), ("meu rh", "Meu RH"), ("vitae|gestao de pessoas", "Gestão de Pessoas (Vitae)"),
         ("saude e seguranca", "Saúde e Segurança do Trabalho"), ("saude hospitais", "Saúde Hospitais e Clínicas"),
         ("ponto eletronico", "Ponto Eletrônico"), ("estoque", "Gestão de Estoque, Compras e Faturamento"),
         ("portal de imoveis", "Portal de Imóveis"), ("gestao de imoveis", "Gestão de Imóveis"), ("obras e projetos", "Obras e Projetos"),
         ("educonnect", "Meu eduCONNECT"), (r"portal (de )?gestao educac", "Portal de Gestão Educacional"),
         ("portal do professor", "Portal do Professor/Coordenador"), ("portais educacionais", "Portais Educacionais"),
         ("processo seletivo", "Processo Seletivo"), ("educacional", "Educacional"),
         (r"assinatura eletronica|^tae\b", "Assinatura Eletrônica (TAE)"), ("consignado", "Consignado Marketplace"),
         ("bibliotec", "Gestão Bibliotecária")]


def area(nome):
    n = "".join(c for c in unicodedata.normalize("NFD", nome.lower()) if not unicodedata.combining(c))
    for rx, rot in AREAS:
        if re.search(rx, n):
            return rot
    return re.sub(r"^totvs\s+", "", re.sub(r"\s+", " ", nome).strip(), flags=re.I)


def baixar_novidades():
    html = get(NOV + "/")
    if not html:
        raise Falha("site de novidades indisponível")
    fila, vistos = re.findall(r"assets/([\w.-]+\.js)", html), set()
    while fila and len(vistos) < 30:
        nome = fila.pop(0)
        if nome in vistos:
            continue
        vistos.add(nome)
        txt = get(f"{NOV}/assets/{nome}") or ""
        ini = txt.find('[{"id":')
        if ini >= 0 and '"release":"12.1.' in txt[ini:ini + 80]:
            fim = txt.index("}]`", ini) + 2
            bruto = json.loads(re.sub(r"\\([\\`$])", r"\1", txt[ini:fim]))
            return [{"id": o["id"], "r": o["release"].split(".")[-1], "s": o.get("segmento", ""), "m": o.get("time", ""),
                     "a": area(o.get("time", "")), "n": o.get("novidade", ""), "d": o.get("descricao", ""),
                     "c": o.get("classificacao", ""), "g": o.get("tipoGanho", ""),
                     "l": o.get("links") or ([o["link"]] if o.get("link") else [])} for o in bruto]
        fila += re.findall(r"[\"'/]([\w.-]+\.js)[\"']", txt)
        time.sleep(.2)
    raise Falha("catálogo de novidades não encontrado no site")


def chave(i):
    return (i["r"], i["p"], i["m"], i["o"], tuple(i["t"]), i["d"])


def so_em(a, b):
    resto = Counter(chave(i) for i in b)
    saida = []
    for i in a:
        k = chave(i)
        if resto[k]:
            resto[k] -= 1
        else:
            saida.append([i["r"], i["p"], i["m"], i["o"], i["t"], i["d"]])
    return saida


def montar_dados(itens, nov, meta, since, hist):
    mods, rots = sorted({i["m"] for i in itens}), sorted({i["o"] for i in itens})
    files = [w for w, _ in Counter(f for i in itens for f in i["f"]).most_common()]
    mi, ri, fi = [{v: n for n, v in enumerate(x)} for x in (mods, rots, files)]
    rows = [[i["r"], i["p"], mi[i["m"]], ri[i["o"]], i["t"], i["d"], [fi[f] for f in i["f"]]] for i in itens]
    novs = [{k: v for k, v in n.items() if k != "id"} for n in nov]
    return {"meta": meta, "mods": mods, "rots": rots, "files": files, "rows": rows, "nov": novs, "since": since, "hist": hist}


def check():
    if PEND.exists():
        shutil.rmtree(PEND)
    est = ler(EST / "estado.json")
    rels_ant = (est or {}).get("releases", {})
    nov = baixar_novidades()
    nov_hash = hashlib.sha256(json.dumps(sorted(nov, key=lambda n: n["id"]), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    novas, atual = {}, {}
    for rel in sorted(set(rels_ant) | {n["r"] for n in nov}):
        pid = rels_ant.get(rel, {}).get("id")
        if not pid:
            pag = get(f"{TDN}/display/public/LRM/Release+notes+dos+patches+12.1.{rel}+-+por+produto")
            m = re.search(r'name="ajs-page-id"\s+content="(\d+)"', pag or "")
            if not m:
                continue
            pid = m.group(1)
        info = json.loads(get(f"{TDN}/rest/api/content/{pid}?expand=version") or "null")
        if not info:
            raise Falha(f"página da release {rel} não encontrada")
        v = info["version"]
        atual[rel] = {"id": pid, "v": v["number"], "when": v["when"][:16]}
        if rels_ant.get(rel, {}).get("v") != v["number"]:
            novas[rel] = pid
        time.sleep(.3)
    if not atual:
        raise Falha("nenhuma release encontrada na TDN")
    if est and not novas and est.get("nov_hash") == nov_hash:
        return {"status": "SEM_MUDANCA"}

    itens_rel = {}
    for rel in atual:
        if rel in novas:
            doc = json.loads(get(f"{TDN}/rest/api/content/{novas[rel]}?expand=body.view,version"))
            itens_rel[rel] = parse_release(rel, doc["body"]["view"]["value"])
            atual[rel].update(v=doc["version"]["number"], when=doc["version"]["when"][:16])
            ant = ler(EST / f"itens_{rel}.json", [])
            if not itens_rel[rel] or len(itens_rel[rel]) < .9 * len(ant):
                raise Falha(f"release {rel} caiu de {len(ant)} para {len(itens_rel[rel])} itens")
            time.sleep(1)
        else:
            itens_rel[rel] = ler(EST / f"itens_{rel}.json")
    nov_ant = ler(EST / "nov.json", [])
    if len(nov) < .9 * len(nov_ant):
        raise Falha(f"novidades caíram de {len(nov_ant)} para {len(nov)}")

    agora = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    hist = ler(EST / "historico.json", [])
    since = (est or {}).get("since") or agora[:16]
    resumo = "primeira carga"
    entrada = None
    if est:
        add, rem = [], []
        for rel in novas:
            ant = ler(EST / f"itens_{rel}.json", [])
            add += so_em(itens_rel[rel], ant)
            rem += so_em(ant, itens_rel[rel])
        add.sort(key=lambda x: (x[0], x[1]))
        rem.sort(key=lambda x: (x[0], x[1]))
        camp = ("r", "s", "a", "n", "d", "c", "g")
        velho, novo = {n["id"]: n for n in nov_ant}, {n["id"]: n for n in nov}
        cur = lambda n: {"r": n["r"], "a": n["a"], "n": n["n"], "c": n["c"], "l": n["l"][:1]}
        nadd = [cur(n) for i, n in novo.items() if i not in velho]
        nupd = [cur(n) for i, n in novo.items() if i in velho and any(n[c] != velho[i][c] for c in camp)]
        nrem = [cur(n) for i, n in velho.items() if i not in novo]
        if add or rem or nadd or nupd or nrem:
            entrada = {"id": agora, "nAdd": len(add), "nRem": len(rem), "add": add[:MAX_ITENS], "rem": rem[:MAX_ITENS],
                       "nov": {"add": nadd, "upd": nupd, "rem": nrem}}
            hist = [entrada] + hist[:MAX_HIST - 1]
            ps = sorted({f"{a[0]}.{a[1]}" for a in add})
            resumo = f"{len(add)} alterações novas, {len(rem)} removidas, {len(nadd)} novidades novas, {len(nupd)} novidades editadas; patches: {', '.join(ps[:12])}"
        else:
            # A página mudou de versão sem mudar o conteúdo: só guarda o novo estado.
            gravar(EST / "estado.json", {"releases": atual, "nov_hash": nov_hash, "since": since})
            return {"status": "SEM_MUDANCA", "obs": "versão da página mudou, conteúdo igual"}

    todos = [i for rel in sorted(atual) for i in itens_rel[rel]]
    gravar(PEND / "data.json", montar_dados(todos, [n for n in nov], atual, since, hist))
    gravar(PEND / "estado.json", {"releases": atual, "nov_hash": nov_hash, "since": since})
    gravar(PEND / "historico.json", hist)
    gravar(PEND / "nov.json", nov)
    for rel in novas:
        gravar(PEND / f"itens_{rel}.json", itens_rel[rel])
    return {"status": "PUBLICAR", "resumo": resumo, "dados": str(PEND / "data.json"), "pagina": str(BASE / "portal-patches-rm.html")}


def commit():
    if not (PEND / "estado.json").exists():
        raise Falha("não há atualização pendente")
    EST.mkdir(exist_ok=True)
    shutil.copy2(PEND / "data.json", BASE / "data.json")
    (PEND / "data.json").unlink()
    estado = PEND / "estado.json"
    for f in PEND.iterdir():
        if f != estado:
            shutil.move(str(f), str(EST / f.name))
    shutil.move(str(estado), str(EST / "estado.json"))  # por último: marca a atualização como concluída
    shutil.rmtree(PEND)
    return {"status": "CONFIRMADO"}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if cmd not in ("check", "commit"):
            raise Falha("use: check ou commit")
        saida = check() if cmd == "check" else commit()
    except Falha as e:
        saida = {"status": "FALHA", "motivo": str(e)}
    except Exception as e:
        saida = {"status": "FALHA", "motivo": f"{type(e).__name__}: {e}"[:300]}
    print(json.dumps(saida, ensure_ascii=False))
    sys.exit(2 if saida["status"] == "FALHA" else 0)
