"""
Database layer (DuckDB). No Streamlit here.

Local by default: creates lae.duckdb next to the app.
MotherDuck: set the environment variable MOTHERDUCK_TOKEN (on Streamlit Cloud,
a top-level key in Secrets becomes an environment variable automatically).
"""
import os
from datetime import datetime
import duckdb

RUTA_LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lae.duckdb")
NOMBRE_MOTHERDUCK = "lae"

ESQUEMA = """
CREATE TABLE IF NOT EXISTS facturas (
    uuid            VARCHAR PRIMARY KEY,   -- Número de Autorización
    numero_dte      VARCHAR,
    fecha           DATE,                  -- fecha de emisión
    nit_emisor      VARCHAR,
    nombre_emisor   VARCHAR,
    nit_receptor    VARCHAR,
    nombre_escuela  VARCHAR,
    departamento    VARCHAR,
    municipio       VARCHAR,
    archivo         VARCHAR,
    fecha_carga     TIMESTAMP
);

CREATE TABLE IF NOT EXISTS lineas (
    uuid             VARCHAR,              -- links to facturas.uuid
    num_linea        INTEGER,
    descripcion      VARCHAR,
    producto         VARCHAR,              -- NULL when unmatched
    categoria        VARCHAR,              -- agricultura | abarrotes | unmatched
    unidad           VARCHAR,              -- libra, unidad, manojo... NULL if unknown
    cantidad         DOUBLE,
    precio_unitario  DECIMAL(12, 4),
    total            DECIMAL(12, 2),
    revisar          BOOLEAN,              -- TRUE = left out of price statistics
    PRIMARY KEY (uuid, num_linea)
);
"""


def conectar(solo_lectura=False):
    """Use as: with conectar() as con: ..."""
    token = os.environ.get("MOTHERDUCK_TOKEN") or os.environ.get("motherduck_token")
    if token:
        con = duckdb.connect(f"md:?motherduck_token={token}")
        con.execute(f"CREATE DATABASE IF NOT EXISTS {NOMBRE_MOTHERDUCK}")
        con.execute(f"USE {NOMBRE_MOTHERDUCK}")
        return con
    return duckdb.connect(RUTA_LOCAL, read_only=solo_lectura)


def crear_tablas(con):
    con.execute(ESQUEMA)


def guardar_facturas(con, facturas, departamento, municipio):
    """
    Saves classified facturas in one transaction, skipping ones already in the database.
    Returns {'nuevas': n, 'duplicadas': n, 'sin_uuid': [archivos]}.
    """
    sin_uuid = [f['archivo'] for f in facturas if not f.get('autorizacion')]

    # Keep one copy per UUID (the same PDF can be uploaded twice in one batch)
    por_uuid = {}
    for f in facturas:
        if f.get('autorizacion'):
            por_uuid.setdefault(f['autorizacion'], f)

    existentes = set()
    if por_uuid:
        existentes = {r[0] for r in con.execute(
            "SELECT uuid FROM facturas WHERE list_contains(?, uuid)", [list(por_uuid)]).fetchall()}
    nuevas = [f for u, f in por_uuid.items() if u not in existentes]
    duplicadas = len(facturas) - len(sin_uuid) - len(nuevas)

    ahora = datetime.now()
    filas_facturas = [
        (f['autorizacion'], f['dte'], f['fecha'], f['nit_emisor'], f['nombre_emisor'],
         f['nit_receptor'], f['nombre_escuela'], departamento, municipio, f['archivo'], ahora)
        for f in nuevas
    ]
    filas_lineas = [
        (f['autorizacion'], i, l['descripcion'], l['producto'], l['categoria'], l['unidad'],
         l['cantidad'], l['precio_unitario'], l['total'], l['revisar'])
        for f in nuevas for i, l in enumerate(f['lineas'], start=1)
    ]

    if nuevas:
        con.begin()
        try:
            con.executemany("INSERT INTO facturas VALUES (?,?,?,?,?,?,?,?,?,?,?)", filas_facturas)
            if filas_lineas:
                con.executemany("INSERT INTO lineas VALUES (?,?,?,?,?,?,?,?,?,?)", filas_lineas)
            con.commit()
        except Exception:
            con.rollback()
            raise

    return {'nuevas': len(nuevas), 'duplicadas': duplicadas, 'sin_uuid': sin_uuid}
