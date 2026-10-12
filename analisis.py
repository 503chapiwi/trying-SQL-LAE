"""Texts included in the SQL/R download (data dictionary and example scripts)."""

DICCIONARIO = """# Diccionario de datos: datos_lae.csv

Una fila por **línea de producto** de cada factura. Todas las facturas guardadas, de todos
los departamentos. Separador: coma. Codificación: UTF-8. Fechas: AAAA-MM-DD.

| Columna | Descripción |
|---|---|
| uuid | Número de autorización de la factura (identificador único) |
| numero_dte | Número de DTE |
| fecha | Fecha de emisión de la factura |
| anio, mes | Año y mes de la fecha de emisión |
| departamento, municipio | Seleccionados al procesar las facturas |
| nit_receptor, nombre_escuela | Escuela (Consejo Educativo) que compró |
| nit_emisor, nombre_emisor | Proveedor |
| num_linea | Número de la línea dentro de la factura |
| descripcion | Texto original de la factura |
| producto | Nombre estandarizado del producto (vacío si no se clasificó) |
| unidad | Unidad de medida tomada de la descripción: libra, unidad, manojo, bolsa... (vacío si no se reconoció) |
| categoria | agricultura (agricultura familiar), abarrotes, o unmatched (sin clasificar) |
| cantidad | Cantidad comprada, en la unidad de la columna `unidad` |
| precio_unitario | Precio por unidad en quetzales, con IVA |
| total | Total de la línea en quetzales |
| revisar | TRUE si cantidad x precio no coincide con el total; excluir de los cálculos de precio |

## Reglas para el análisis

- **Agrupe siempre por producto y unidad.** Sumar libras con unidades, o promediar el precio
  de una bolsa con el de una unidad, no tiene sentido.
- **Para precios, excluya `revisar = TRUE`.** Para totales en quetzales, inclúyalas.
- Las líneas con `categoria = unmatched` no tienen producto; no entran en los totales de MAGA.
- Lea `numero_dte`, `nit_receptor` y `nit_emisor` como texto: algunos NIT terminan en K.
  Los ejemplos de SQL y R ya lo hacen.

## productos.csv

Lista de todos los productos con su categoría y una columna `grupo` vacía. Para analizar por
grupos (frutas, verduras, carnes, lácteos, chiles, panadería...), llene `grupo` y únalo a
datos_lae.csv por la columna `producto`. Hay un ejemplo al final de ejemplo.sql.
"""

EJEMPLO_SQL = """-- Ejemplos con DuckDB. Abra DuckDB en la carpeta donde está datos_lae.csv
-- y ejecute las consultas una por una, o todas con:  .read ejemplo.sql

CREATE OR REPLACE VIEW datos AS
SELECT *, producto || ' (' || coalesce(unidad, 'sin unidad') || ')' AS producto_unidad
FROM read_csv('datos_lae.csv', types = {'numero_dte': 'VARCHAR', 'nit_receptor': 'VARCHAR', 'nit_emisor': 'VARCHAR'})
WHERE producto IS NOT NULL;

-- 1a. Cantidad de cada producto por municipio, en un departamento
SELECT municipio, producto_unidad, sum(cantidad) AS cantidad
FROM datos
WHERE departamento = 'Totonicapán'
  AND producto IN ('tomate', 'banano', 'huevo')
GROUP BY ALL ORDER BY producto_unidad, cantidad DESC;

-- 1b. Cantidad de cada producto por departamento, todo el país
SELECT departamento, producto_unidad, sum(cantidad) AS cantidad
FROM datos
WHERE producto IN ('tomate', 'banano', 'huevo')
GROUP BY ALL ORDER BY producto_unidad, cantidad DESC;

-- 2a. Precio mediano de cada producto por municipio, en un departamento
SELECT municipio, producto_unidad, median(precio_unitario) AS precio_mediano, count(*) AS compras
FROM datos
WHERE NOT revisar
  AND departamento = 'Totonicapán'
  AND producto IN ('tomate', 'banano', 'huevo')
GROUP BY ALL ORDER BY producto_unidad, precio_mediano;

-- 2b. Precio mediano de cada producto por departamento, todo el país
SELECT departamento, producto_unidad, median(precio_unitario) AS precio_mediano, count(*) AS compras
FROM datos
WHERE NOT revisar
  AND producto IN ('tomate', 'banano', 'huevo')
GROUP BY ALL ORDER BY producto_unidad, precio_mediano;

-- 3. (Más adelante) Por grupo de producto: llene la columna 'grupo' en productos.csv y quite los --
-- SELECT d.departamento, p.grupo, d.unidad,
--        sum(d.cantidad) AS cantidad, median(d.precio_unitario) FILTER (WHERE NOT d.revisar) AS precio_mediano
-- FROM datos d JOIN read_csv('productos.csv') p USING (producto, categoria)
-- GROUP BY ALL ORDER BY p.grupo, d.departamento;
"""

EJEMPLO_R = """# Ejemplos en R. Ponga este archivo en la misma carpeta que datos_lae.csv.
# Paquetes necesarios (una sola vez): install.packages(c("readr", "dplyr", "ggplot2"))
library(readr)
library(dplyr)
library(ggplot2)

# NITs and DTE numbers are read as text: some NITs end in K
datos <- read_csv("datos_lae.csv", show_col_types = FALSE,
                  col_types = cols(numero_dte = "c", nit_receptor = "c", nit_emisor = "c")) |>
  filter(!is.na(producto)) |>
  mutate(producto_unidad = paste0(producto, " (", coalesce(unidad, "sin unidad"), ")"))

# --- Cambie estos valores ---
# (Si un filtro con tilde como "Totonicapán" no devuelve nada, abra este archivo
#  con codificación UTF-8: en RStudio, File > Reopen with Encoding > UTF-8.)
departamento_elegido <- "Totonicapán"
productos_elegidos   <- c("tomate", "banano", "huevo")

seleccion <- datos |> filter(producto %in% productos_elegidos)

grafica <- function(df, x, y, titulo, eje_y) {
  ggplot(df, aes(x = .data[[x]], y = .data[[y]])) +
    geom_col(fill = "#1D9E75") +
    coord_flip() +
    facet_wrap(~ producto_unidad, scales = "free_x") +
    labs(title = titulo, x = NULL, y = eje_y) +
    theme_minimal()
}

# 1a. Cantidad de cada producto por municipio, en un departamento
cantidad_municipio <- seleccion |>
  filter(departamento == departamento_elegido) |>
  group_by(municipio, producto_unidad) |>
  summarise(cantidad = sum(cantidad, na.rm = TRUE), .groups = "drop")
g1a <- grafica(cantidad_municipio, "municipio", "cantidad",
               paste("Cantidad comprada por municipio:", departamento_elegido), "Cantidad")

# 1b. Cantidad de cada producto por departamento, todo el país
cantidad_depto <- seleccion |>
  group_by(departamento, producto_unidad) |>
  summarise(cantidad = sum(cantidad, na.rm = TRUE), .groups = "drop")
g1b <- grafica(cantidad_depto, "departamento", "cantidad",
               "Cantidad comprada por departamento", "Cantidad")

# 2a. Precio mediano por municipio, en un departamento (sin líneas marcadas para revisión)
precio_municipio <- seleccion |>
  filter(!revisar, departamento == departamento_elegido) |>
  group_by(municipio, producto_unidad) |>
  summarise(precio_mediano = median(precio_unitario, na.rm = TRUE), compras = n(), .groups = "drop")
g2a <- grafica(precio_municipio, "municipio", "precio_mediano",
               paste("Precio mediano por municipio:", departamento_elegido), "Quetzales")

# 2b. Precio mediano por departamento, todo el país
precio_depto <- seleccion |>
  filter(!revisar) |>
  group_by(departamento, producto_unidad) |>
  summarise(precio_mediano = median(precio_unitario, na.rm = TRUE), compras = n(), .groups = "drop")
g2b <- grafica(precio_depto, "departamento", "precio_mediano",
               "Precio mediano por departamento", "Quetzales")

print(g1a); print(g1b); print(g2a); print(g2b)

# Para guardarlas como imagen:
# ggsave("cantidad_municipio.png", g1a, width = 10, height = 6)
"""
