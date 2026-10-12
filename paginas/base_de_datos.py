import hmac
import io
import zipfile
import streamlit as st

from db import conectar, crear_tablas

st.set_page_config(layout="wide")
st.title("📊 Base de datos de facturas LAE")

# --- PASSWORD (set CLAVE_DATOS in Streamlit Secrets) ---
if not st.session_state.get("autorizado"):
    try:
        clave_correcta = st.secrets.get("CLAVE_DATOS")
    except Exception:
        clave_correcta = None
    if not clave_correcta:
        st.error("Esta página no está configurada: falta CLAVE_DATOS en los Secrets.")
        st.stop()
    clave = st.text_input("Contraseña", type="password")
    if clave and hmac.compare_digest(clave, str(clave_correcta)):
        st.session_state["autorizado"] = True
        st.rerun()
    elif clave:
        st.error("Contraseña incorrecta.")
    st.stop()

DINERO = st.column_config.NumberColumn(format="Q %.2f")


@st.cache_data(ttl=60, show_spinner=False)
def q(sql, params=None):
    """Runs a SQL query and returns a DataFrame. Cached for 60 seconds."""
    with conectar() as con:
        crear_tablas(con)
        return con.execute(sql, params or []).df()


# Not cached: an empty result must never be remembered after receipts are saved
with conectar() as con:
    crear_tablas(con)
    total_facturas = con.execute("SELECT count(*) FROM facturas").fetchone()[0]

if total_facturas == 0:
    st.info("Todavía no hay facturas guardadas. Procese facturas en la página "
            "**Procesar facturas** y vuelva aquí.")
    st.stop()

# --- FILTERS ---
TODOS = "Todos los departamentos"
st.sidebar.header("Filtros")
deptos = q("SELECT DISTINCT departamento FROM facturas ORDER BY 1")["departamento"].tolist()
depto = st.sidebar.selectbox("Departamento", [TODOS] + deptos, index=1 if len(deptos) == 1 else 0)
deptos_sel = deptos if depto == TODOS else [depto]

# With one department, results are shown by municipality; with all, by department
if depto == TODOS:
    NIVEL, NIVEL_ETQ = "departamento", "Departamento"
    munis_sel, todos_munis = [], True
else:
    NIVEL, NIVEL_ETQ = "municipio", "Municipio"
    munis = q("SELECT DISTINCT municipio FROM facturas WHERE departamento = ? ORDER BY 1",
              [depto])["municipio"].tolist()
    munis_sel = st.sidebar.multiselect("Municipio", munis, placeholder="Todos")
    todos_munis = not munis_sel

rango = q("""SELECT min(coalesce(fecha, CAST(fecha_carga AS DATE))) AS desde,
                    max(coalesce(fecha, CAST(fecha_carga AS DATE))) AS hasta
             FROM facturas WHERE list_contains(?, departamento)""", [deptos_sel]).iloc[0]
fechas = st.sidebar.date_input("Fechas de emisión", (rango["desde"], rango["hasta"]))
desde, hasta = (fechas[0], fechas[-1]) if isinstance(fechas, tuple) and fechas else (rango["desde"], rango["hasta"])

if st.sidebar.button("🔄 Actualizar datos"):
    st.cache_data.clear()

# Every query below starts from this filtered set of product lines
BASE = """
WITH base AS (
    SELECT l.*, f.departamento, f.municipio, f.nit_emisor, f.nit_receptor,
           f.nombre_escuela, f.nombre_emisor,
           coalesce(f.fecha, CAST(f.fecha_carga AS DATE)) AS fecha
    FROM lineas l JOIN facturas f USING (uuid)
    WHERE list_contains(?, f.departamento)
      AND (? OR list_contains(?, f.municipio))
      AND coalesce(f.fecha, CAST(f.fecha_carga AS DATE)) BETWEEN ? AND ?
)
"""
P = [deptos_sel, todos_munis, munis_sel, desde, hasta]

# --- SUMMARY ---
res = q(BASE + """
SELECT coalesce(sum(total) FILTER (WHERE categoria <> 'unmatched'), 0)::DOUBLE AS gasto,
       coalesce(sum(total) FILTER (WHERE categoria = 'agricultura'), 0)::DOUBLE AS agri,
       count(DISTINCT uuid)          AS facturas,
       count(DISTINCT nit_receptor)  AS escuelas,
       count(DISTINCT nit_emisor)    AS proveedores
FROM base""", P).iloc[0]

pct_agri = res["agri"] / res["gasto"] if res["gasto"] else 0
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Gasto clasificado", f"Q {res['gasto']:,.2f}")
c2.metric("Agricultura familiar", f"{pct_agri:.0%}")
c3.metric("Facturas", f"{int(res['facturas']):,}")
c4.metric("Escuelas", f"{int(res['escuelas']):,}")
c5.metric("Proveedores", f"{int(res['proveedores']):,}")

if res["facturas"] == 0:
    st.warning("No hay facturas con estos filtros.")
    st.stop()

# --- SPENDING ---
st.subheader(f"Gasto por {NIVEL_ETQ.lower()}")
gasto = q(BASE + f"""
SELECT {NIVEL} AS "{NIVEL_ETQ}",
       coalesce(sum(total) FILTER (WHERE categoria = 'agricultura'), 0)::DOUBLE AS "Agricultura familiar",
       coalesce(sum(total) FILTER (WHERE categoria = 'abarrotes'), 0)::DOUBLE  AS "Abarrotes",
       count(DISTINCT uuid) AS Facturas
FROM base GROUP BY 1 ORDER BY 1""", P)
gasto["% Agric. familiar"] = (gasto["Agricultura familiar"] /
                              (gasto["Agricultura familiar"] + gasto["Abarrotes"])).fillna(0) * 100

st.bar_chart(gasto, x=NIVEL_ETQ, y=["Agricultura familiar", "Abarrotes"],
             color=["#1D9E75", "#B4B2A9"], y_label="Quetzales")
st.dataframe(gasto, hide_index=True, width="stretch", column_config={
    "Agricultura familiar": DINERO, "Abarrotes": DINERO,
    "% Agric. familiar": st.column_config.NumberColumn(format="%.0f%%"),
})

# --- PRICES BY PRODUCT ---
st.subheader("Precios por producto")
st.caption("Precio unitario por producto y unidad de medida. Las líneas marcadas para revisión no se incluyen.")
precios = q(BASE + """
SELECT producto AS Producto, coalesce(unidad, 'sin unidad') AS Unidad, categoria AS "Categoría",
       count(*) AS Compras,
       median(precio_unitario)::DOUBLE AS Mediana,
       min(precio_unitario)::DOUBLE    AS "Mínimo",
       max(precio_unitario)::DOUBLE    AS "Máximo",
       sum(cantidad)                   AS "Cantidad total",
       sum(total)::DOUBLE              AS "Gasto total"
FROM base
WHERE NOT revisar AND producto IS NOT NULL
GROUP BY ALL ORDER BY "Gasto total" DESC""", P)
st.dataframe(precios, hide_index=True, width="stretch", column_config={
    "Mediana": DINERO, "Mínimo": DINERO, "Máximo": DINERO, "Gasto total": DINERO,
    "Cantidad total": st.column_config.NumberColumn(format="%.1f"),
})

# --- COMPARE SEVERAL PRODUCTS ---
if not precios.empty:
    st.subheader(f"Comparar productos entre {'departamentos' if NIVEL == 'departamento' else 'municipios'}")
    etiquetas = [f"{p} ({u})" for p, u in zip(precios["Producto"], precios["Unidad"])]
    elegidos = st.multiselect("Productos", etiquetas, default=etiquetas[:3],
                              help="Cada producto se muestra con su unidad de medida, "
                                   "porque precios en libras y en unidades no se pueden comparar.")
    medida = st.radio("Mostrar", ["Precio mediano", "Cantidad total", "Gasto total"], horizontal=True)

    if elegidos:
        pares = [[precios["Producto"][etiquetas.index(e)], precios["Unidad"][etiquetas.index(e)]]
                 for e in elegidos]
        comp = q(BASE + f"""
        SELECT {NIVEL} AS "{NIVEL_ETQ}",
               producto || ' (' || coalesce(unidad, 'sin unidad') || ')' AS Producto,
               count(*)                        AS Compras,
               median(precio_unitario)::DOUBLE AS "Precio mediano",
               sum(cantidad)                   AS "Cantidad total",
               sum(total)::DOUBLE              AS "Gasto total"
        FROM base
        WHERE NOT revisar
          AND list_contains(?, [producto, coalesce(unidad, 'sin unidad')])
        GROUP BY ALL ORDER BY 1, 2""", P + [pares])

        st.bar_chart(comp, x=NIVEL_ETQ, y=medida, color="Producto", stack=False,
                     y_label=medida if medida == "Cantidad total" else f"{medida} (Q)")
        tabla = comp.pivot(index=NIVEL_ETQ, columns="Producto", values=medida).reset_index()
        formato = st.column_config.NumberColumn(format="%.1f" if medida == "Cantidad total" else "Q %.2f")
        st.dataframe(tabla, hide_index=True, width="stretch",
                     column_config={c: formato for c in tabla.columns if c != NIVEL_ETQ})

# --- REVIEW ---
sin_clasificar = q(BASE + """
SELECT descripcion AS "Descripción", departamento AS Departamento, municipio AS Municipio,
       count(*) AS Veces, sum(total)::DOUBLE AS Total
FROM base WHERE categoria = 'unmatched'
GROUP BY ALL ORDER BY Total DESC""", P)
revisar = q(BASE + "SELECT count(*) AS n FROM base WHERE revisar AND producto IS NOT NULL", P)["n"][0]

with st.expander(f"Productos sin clasificar ({len(sin_clasificar)}) · líneas excluidas de precios ({revisar})"):
    st.caption("Estos productos no entran en los totales. Para clasificarlos, agregue la palabra "
               "a CULTIVADOS o ABARROTES en clasificacion.py.")
    st.dataframe(sin_clasificar, hide_index=True, width="stretch",
                 column_config={"Total": DINERO})

# --- DOWNLOADS ---
st.subheader("Descargas")
col_x, col_r = st.columns(2)

detalle = q(BASE + """
SELECT fecha AS Fecha, departamento AS Departamento, municipio AS Municipio,
       nombre_escuela AS Escuela, nombre_emisor AS Proveedor, nit_emisor AS "NIT Proveedor",
       descripcion AS "Descripción", producto AS Producto, categoria AS "Categoría",
       unidad AS Unidad, cantidad AS Cantidad,
       precio_unitario::DOUBLE AS "Precio unitario", total::DOUBLE AS Total,
       revisar AS Revisar, uuid AS "Número de autorización"
FROM base ORDER BY fecha, departamento, municipio, uuid""", P)
buf = io.BytesIO()
detalle.to_excel(buf, index=False, sheet_name="Datos")
col_x.download_button("📥 Datos filtrados (Excel)", data=buf.getvalue(),
                      file_name=f"Datos_LAE_{depto}_{desde}_{hasta}.xlsx",
                      mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
col_x.caption("Lo que muestra la página con los filtros actuales.")


@st.cache_data(ttl=60, show_spinner=False)
def paquete_analisis():
    """Zip with ALL saved data (ignores the filters) for SQL and R."""
    datos = q("""
    SELECT f.uuid, f.numero_dte, f.fecha,
           year(f.fecha) AS anio, month(f.fecha) AS mes,
           f.departamento, f.municipio,
           f.nit_receptor, f.nombre_escuela, f.nit_emisor, f.nombre_emisor,
           l.num_linea, regexp_replace(l.descripcion, '\\s+', ' ', 'g') AS descripcion, l.producto, l.unidad, l.categoria,
           l.cantidad, l.precio_unitario::DOUBLE AS precio_unitario,
           l.total::DOUBLE AS total, l.revisar
    FROM lineas l JOIN facturas f USING (uuid)
    ORDER BY f.departamento, f.municipio, f.fecha, f.uuid, l.num_linea""")
    productos = q("""
    SELECT producto, categoria, '' AS grupo, count(*) AS lineas
    FROM lineas WHERE producto IS NOT NULL
    GROUP BY producto, categoria ORDER BY categoria, producto""")

    from analisis import DICCIONARIO, EJEMPLO_SQL, EJEMPLO_R
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        datos["revisar"] = datos["revisar"].map({True: "TRUE", False: "FALSE"})
        z.writestr("datos_lae.csv", datos.to_csv(index=False, date_format="%Y-%m-%d"))
        z.writestr("productos.csv", productos.to_csv(index=False))
        z.writestr("diccionario_datos.md", DICCIONARIO)
        z.writestr("ejemplo.sql", EJEMPLO_SQL)
        z.writestr("ejemplo.R", EJEMPLO_R)
    return zbuf.getvalue()


col_r.download_button("📦 Todos los datos para SQL / R (.zip)", data=paquete_analisis(),
                      file_name="datos_lae_analisis.zip", mime="application/zip")
col_r.caption("Todas las facturas guardadas, de todos los departamentos, sin filtros. "
              "Incluye el CSV, un diccionario de datos y ejemplos en SQL y R.")
