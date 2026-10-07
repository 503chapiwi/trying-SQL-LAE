import duckdb

with duckdb.connect("lae.duckdb", read_only=True) as con:
    con.sql("SELECT count(*) AS facturas FROM facturas").show()
    con.sql("""
        SELECT producto, unidad, count(*) AS n, median(precio_unitario) AS precio_mediano
        FROM lineas
        WHERE NOT revisar AND producto IS NOT NULL
        GROUP BY ALL ORDER BY n DESC
    """).show()
