#!/usr/bin/env python3
"""
Construye la base de datos SQLite del Padrón Nacional Electoral de Costa Rica
a partir de los archivos oficiales del TSE.

Fuente oficial: https://www.tse.go.cr/zip/padron/padron_completo.zip
Contiene:
  - PADRON_COMPLETO.txt : ~3.7M electores inscritos
  - distelec.txt        : catálogo de provincia/cantón/distrito por código electoral

Uso:
  python3 build_db.py                # descarga el ZIP del TSE y construye padron.db
  python3 build_db.py --zip ruta.zip # usa un ZIP ya descargado
  python3 build_db.py --data-dir d   # usa PADRON_COMPLETO.txt/distelec.txt ya extraídos
"""
import argparse
import csv
import io
import os
import sqlite3
import sys
import time
import urllib.request
import zipfile

PADRON_URL = "https://www.tse.go.cr/zip/padron/padron_completo.zip"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "padron.db")
ENCODING = "latin-1"  # los archivos del TSE vienen en ISO-8859-1


def log(msg):
    print(f"[build_db] {msg}", flush=True)


def download_zip(dest):
    log(f"Descargando padrón oficial del TSE: {PADRON_URL}")
    req = urllib.request.Request(PADRON_URL, headers={"User-Agent": UA})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        total = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
            print(f"\r  {total/1e6:6.1f} MB", end="", flush=True)
    print()
    log(f"Descarga completa en {time.time()-t0:.1f}s ({total/1e6:.1f} MB)")


def open_texts(zip_path=None, data_dir=None):
    """Devuelve (padron_lines_iter, distelec_lines_iter) como texto decodificado."""
    if data_dir:
        pc = os.path.join(data_dir, "PADRON_COMPLETO.txt")
        de = os.path.join(data_dir, "distelec.txt")
        return (
            open(pc, encoding=ENCODING),
            open(de, encoding=ENCODING),
        )
    zf = zipfile.ZipFile(zip_path)
    names = {n.lower(): n for n in zf.namelist()}
    pc = names.get("padron_completo.txt")
    de = names.get("distelec.txt")
    if not pc or not de:
        raise SystemExit(f"ZIP no contiene los archivos esperados: {zf.namelist()}")
    return (
        io.TextIOWrapper(zf.open(pc), encoding=ENCODING),
        io.TextIOWrapper(zf.open(de), encoding=ENCODING),
    )


def parse_padron(fh):
    """Genera tuplas (cedula, codelec, fecha_caducidad, junta, nombre, ap1, ap2)."""
    for row in csv.reader(fh):
        if len(row) < 8:
            continue
        cedula = row[0].strip()
        codelec = row[1].strip()
        fecha = row[3].strip()
        junta = row[4].strip()
        nombre = row[5].strip()
        ap1 = row[6].strip()
        ap2 = row[7].strip()
        if not cedula:
            continue
        yield (cedula, codelec, fecha, junta, nombre, ap1, ap2)


def parse_distelec(fh):
    for row in csv.reader(fh):
        if len(row) < 4:
            continue
        yield (row[0].strip(), row[1].strip(), row[2].strip(), row[3].strip())


def build(zip_path=None, data_dir=None):
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        CREATE TABLE distelec (
            codelec   TEXT PRIMARY KEY,
            provincia TEXT,
            canton    TEXT,
            distrito  TEXT
        );
        CREATE TABLE padron (
            cedula           TEXT PRIMARY KEY,
            codelec          TEXT,
            fecha_caducidad  TEXT,
            junta            TEXT,
            nombre           TEXT,
            apellido1        TEXT,
            apellido2        TEXT
        );
        """
    )

    padron_fh, distelec_fh = open_texts(zip_path, data_dir)

    log("Cargando distelec...")
    cur.executemany(
        "INSERT OR REPLACE INTO distelec VALUES (?,?,?,?)", parse_distelec(distelec_fh)
    )
    con.commit()

    log("Cargando padrón (esto tarda ~30-60s)...")
    t0 = time.time()
    n = 0
    batch = []
    ins = "INSERT OR REPLACE INTO padron VALUES (?,?,?,?,?,?,?)"
    for rec in parse_padron(padron_fh):
        batch.append(rec)
        if len(batch) >= 50000:
            cur.executemany(ins, batch)
            n += len(batch)
            batch.clear()
            print(f"\r  {n:,} registros", end="", flush=True)
    if batch:
        cur.executemany(ins, batch)
        n += len(batch)
    print()
    con.commit()
    log(f"Insertados {n:,} electores en {time.time()-t0:.1f}s")

    log("Creando índices de búsqueda por nombre...")
    cur.executescript(
        """
        CREATE INDEX idx_ap1 ON padron(apellido1);
        CREATE INDEX idx_ap1_ap2_nom ON padron(apellido1, apellido2, nombre);
        CREATE INDEX idx_nombre ON padron(nombre);
        """
    )
    con.commit()
    cur.execute("PRAGMA optimize")
    con.execute("VACUUM")
    con.close()
    size = os.path.getsize(DB_PATH) / 1e6
    log(f"Listo: {DB_PATH} ({size:.0f} MB)")


def main():
    ap = argparse.ArgumentParser(description="Construye padron.db desde el TSE")
    ap.add_argument("--zip", help="ruta a padron_completo.zip ya descargado")
    ap.add_argument("--data-dir", help="dir con PADRON_COMPLETO.txt y distelec.txt")
    args = ap.parse_args()

    if args.data_dir:
        build(data_dir=args.data_dir)
        return
    zip_path = args.zip
    if not zip_path:
        zip_path = os.path.join(HERE, "padron_completo.zip")
        if not os.path.exists(zip_path):
            download_zip(zip_path)
        else:
            log(f"Usando ZIP existente: {zip_path}")
    build(zip_path=zip_path)


if __name__ == "__main__":
    main()
