"""
Reads one SAT FEL factura PDF and returns its data as a plain dict.
No Streamlit and no Excel here, so it can be reused by the database step.
"""
import re
from datetime import date
import pdfplumber
from texto import normalize_text, clean_currency

# Rows containing any of these are administrative, not products
SKIP_KEYWORDS = ['totales', 'superintendencia', 'datos del certificador',
                 'contribuyendo', 'sujeto a pagos', 'no genera derecho',
                 'descripcion', 'cantidad', 'unitario', 'descuentos', 'impuestos']


MESES = {'ene': 1, 'feb': 2, 'mar': 3, 'abr': 4, 'may': 5, 'jun': 6,
         'jul': 7, 'ago': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dic': 12}

UUID_RE = re.compile(r'\b([0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})\b', re.IGNORECASE)

# First word of the description -> unit. Matched by prefix, so plurals and
# typos like 'unidadad' still map to 'unidad'.
UNIDADES = ['unidad', 'libra', 'manojo', 'bolsa', 'onza', 'cabeza', 'rama', 'raja',
            'botella', 'paquete', 'quintal', 'docena', 'caja', 'arroba', 'lata',
            'frasco', 'galon', 'litro', 'kilo', 'racimo', 'ciento', 'fardo', 'costal',
            'bandeja', 'canasta', 'mazo']

# A line is flagged 'revisar' when cantidad x precio differs from the total by more than this
TOLERANCIA_PRECIO = 0.02


# --- ROW-LEVEL HELPERS (unchanged from the original tool) ---

def extract_value_from_row(row_list, total_idx):
    # PRIMARY: SAT facturas have 'IVA' as the Impuestos column label on every item row.
    # The Total (Q) column always sits immediately to its left. This per-row anchor
    # survives pdfplumber's column-count drift across pages of the same invoice
    # (e.g. when page 2 of a multi-page invoice extracts as 10 columns instead of 12,
    # the header-detected total_idx from page 1 would land on the 'IVA' string).
    for idx, cell in enumerate(row_list):
        if cell is None:
            continue
        if str(cell).strip().upper() == 'IVA' and idx > 0:
            val = clean_currency(row_list[idx - 1])
            if val > 0:
                return val
            break  # IVA found but no usable value before it; don't fall through
                   # to the last-positive heuristic — that would grab the Impuestos value.

    # FALLBACK 1: header-detected total column (correct when IVA marker is absent).
    if total_idx != -1 and len(row_list) > total_idx:
        val = clean_currency(row_list[total_idx])
        if val > 0: return val

    # FALLBACK 2: last positive number (kept for non-SAT formats).
    for item in reversed(row_list):
        val = clean_currency(item)
        if val > 0: return val
    return 0.0


def extract_school_name(text):
    """
    Extracts the school name from the 'Nombre Receptor:' field of a SAT FEL factura.

    In the extracted PDF text the left column (receptor data) and the right column
    (fechas / moneda) share the same lines, e.g.:

        Nombre Receptor: CONSEJO EDUCATIVO, EORM J.M. CANTON Fecha y hora de certificación: ...
        CHUIXCHIMAL
        Moneda: GTQ

    So the name starts after 'Nombre Receptor:' and may continue on following lines
    until a new labeled field appears. Right-column text ('Fecha y hora', 'Moneda:')
    and the alternate-layout 'Dirección comprador:' field are stripped from each line.
    """
    lines = text.split('\n')
    name_parts = []
    started = False
    for line in lines:
        if not started:
            m = re.match(r'\s*Nombre\s*Receptor:\s*(.*)', line, re.IGNORECASE)
            if not m:
                continue
            started = True
            part = m.group(1)
        else:
            # A continuation line; stop at the next labeled field or the items table.
            if line.strip() == '.' or re.match(
                r'(Moneda|#No|NIT|Nit|N[úu]mero|Serie|Fecha|Direcci[óo]n)\b',
                line.strip(), re.IGNORECASE
            ):
                break
            part = line
        part = re.split(r'(?i)Fecha\s*y\s*hora', part)[0]
        part = re.split(r'(?i)Moneda\s*:', part)[0]
        part = re.split(r'(?i)Direcci[óo]n\s*comprador', part)[0]
        part = part.strip()
        if part:
            name_parts.append(part)
        else:
            break
    name = re.sub(r'\s+', ' ', ' '.join(name_parts)).strip()
    name = name.strip('"').strip()  # some names come fully quoted: "ORGANIZACION..."
    return name or "N/A"


def find_description_in_row(row):
    """
    Find the product description in a row by finding the longest text cell
    that contains letters (not pure numbers/symbols).
    """
    best_candidate = ""
    best_score = 0

    for cell in row:
        if cell is None:
            continue

        cell_str = str(cell).strip()
        if not cell_str:
            continue

        # Skip obvious non-descriptions
        cell_upper = cell_str.upper()
        if cell_upper in ['BIEN', 'SERVICIO', 'B/S']:
            continue
        if cell_upper.startswith('IVA ') or cell_upper.startswith('ISR '):
            continue

        # Check if it's a pure number
        try:
            float(cell_str.replace(',', '.').replace(' ', ''))
            continue
        except ValueError:
            pass

        # Score = number of letters (prefer text over numbers)
        letter_count = sum(1 for c in cell_str if c.isalpha())
        if letter_count < 3:
            continue

        if letter_count > best_score:
            best_score = letter_count
            best_candidate = cell_str

    return best_candidate


def merge_split_rows(tables):
    """
    Merges rows that were split due to white lines in PDF tables.

    Detects continuation rows (rows with only description text but no item number/value)
    and merges them back into the previous data row's description.

    Example:
        Row N:   ['23', None, 'Bien', '32', 'UNIDADES DE', '5.50', ..., '176.00']
        Row N+1: [None, '', '', '', 'AGUACATE', '', '', '', '', '']  <- continuation

        After merge:
        Row N:   ['23', None, 'Bien', '32', 'UNIDADES DE AGUACATE', '5.50', ..., '176.00']
        Row N+1: removed
    """
    if not tables:
        return tables

    merged = []
    i = 0
    while i < len(tables):
        current_row = list(tables[i]) if tables[i] else []

        # Look ahead to merge any continuation rows
        j = i + 1
        while j < len(tables):
            next_row = tables[j]
            if not next_row:
                break

            # Continuation row: no item number in first 5 cells, no numeric
            # values anywhere, and at least some text content
            has_item_number = False
            for cell in next_row[:5]:
                if cell:
                    cell_str = str(cell).strip()
                    if cell_str.isdigit():
                        has_item_number = True
                        break

            # The TOTALES row is never a continuation of a product description
            if any(cell and normalize_text(str(cell)).strip().startswith('totales') for cell in next_row):
                break

            has_numeric_value = False
            text_fragments = []
            for cell in next_row:
                if cell is None:
                    continue
                cell_str = str(cell).strip()
                if not cell_str:
                    continue
                try:
                    # Remove thousands commas: '14,146.60' must count as a number.
                    # (Replacing ',' with '.' made it unparseable, so the TOTALES row
                    # was merged into the last product row and that row was dropped.)
                    val = float(cell_str.replace(',', '').replace(' ', ''))
                    if val > 0:
                        has_numeric_value = True
                        break
                except ValueError:
                    cell_upper = cell_str.upper()
                    if len(cell_str) >= 3 and cell_upper not in ['BIEN', 'SERVICIO', 'B/S']:
                        if not cell_upper.startswith('IVA') and not cell_upper.startswith('ISR'):
                            text_fragments.append(cell_str)

            if not has_item_number and not has_numeric_value and text_fragments:
                continuation_text = " ".join(text_fragments)

                # Find description cell in current row and append the continuation
                for k, cell in enumerate(current_row):
                    if cell is None:
                        continue
                    cell_str = str(cell).strip()
                    if not cell_str:
                        continue
                    cell_upper = cell_str.upper()
                    if cell_upper in ['BIEN', 'SERVICIO', 'B/S']:
                        continue
                    if cell_upper.startswith('IVA') or cell_upper.startswith('ISR'):
                        continue
                    try:
                        float(cell_str.replace(',', '.').replace(' ', ''))
                        continue
                    except ValueError:
                        pass
                    if len(cell_str) >= 3:
                        current_row[k] = cell_str + " " + continuation_text
                        break

                j += 1
            else:
                break

        merged.append(current_row)
        i = j

    return merged


# --- DATABASE FIELDS (new in step 2) ---

def _es_numero(cell):
    """True only for cells that are purely a number ('1,327.50', '1.5'), not '500 GRAMOS'."""
    if cell is None:
        return False
    return bool(re.fullmatch(r'\d[\d,]*(\.\d+)?', str(cell).strip()))


def _indice_descripcion(row):
    """Index of the description cell, using the same scoring as find_description_in_row."""
    best_idx, best_score = None, 0
    for idx, cell in enumerate(row):
        if cell is None:
            continue
        cell_str = str(cell).strip()
        if not cell_str or _es_numero(cell_str):
            continue
        cell_upper = cell_str.upper()
        if cell_upper in ['BIEN', 'SERVICIO', 'B/S'] or cell_upper.startswith(('IVA', 'ISR')):
            continue
        letter_count = sum(1 for c in cell_str if c.isalpha())
        if letter_count >= 3 and letter_count > best_score:
            best_idx, best_score = idx, letter_count
    return best_idx


def extraer_cantidad_precio(row_tbl):
    """
    Anchored on the description cell rather than column positions, because columns
    shift between pages: Cantidad is the nearest number BEFORE the description,
    P. Unitario the first number AFTER it. Returns (cantidad, precio) or (None, None).
    """
    d = _indice_descripcion(row_tbl)
    if d is None:
        return None, None
    antes = [c for c in row_tbl[:d] if _es_numero(c)]
    despues = [c for c in row_tbl[d + 1:] if _es_numero(c)]
    cantidad = clean_currency(antes[-1]) if antes else None
    precio = clean_currency(despues[0]) if despues else None
    return cantidad, precio


def necesita_revision(cantidad, precio, total):
    """Flags lines whose cantidad x precio doesn't match the total (bad extraction or a discount)."""
    if not cantidad or not precio:
        return True
    return abs(cantidad * precio - total) > max(0.05, TOLERANCIA_PRECIO * total)


def extraer_unidad(descripcion):
    """'LIBRAS DE TOMATE' -> 'libra', 'unidadad de huevo' -> 'unidad', unknown -> None."""
    m = re.match(r'\s*([a-z]+)', normalize_text(descripcion or ''))
    if not m:
        return None
    for unidad in UNIDADES:
        if m.group(1).startswith(unidad):
            return unidad
    return None


def extraer_fecha(text):
    """'Fecha y hora de emision: 23-mar-2026 21:31:51' -> date(2026, 3, 23)"""
    m = re.search(r'Fecha\s*y\s*hora\s*de\s*emisi[óo]n:\s*(\d{1,2})-([a-zA-Z]{3})-(\d{4})', text, re.IGNORECASE)
    if not m:
        return None
    mes = MESES.get(m.group(2).lower())
    if not mes:
        return None
    try:
        return date(int(m.group(3)), mes, int(m.group(1)))
    except ValueError:
        return None


def extraer_autorizacion(text):
    """The factura's UUID (Número de Autorización). It shares a line with Nit Emisor
    in the extracted text, so it's found by its pattern rather than by its label."""
    m = UUID_RE.search(text)
    return m.group(1).upper() if m else None


# --- FACTURA-LEVEL STEPS (pulled out of the original main loop) ---

def es_factura_estandar(text):
    """A standard SAT factura has at least 2 of 3 markers that proformas/cotizaciones lack."""
    has_dte = bool(re.search(r'N[úu]mero\s*de\s*DTE', text, re.IGNORECASE))
    has_autorizacion = bool(re.search(r'N[úu]mero\s*de\s*Autorizaci[óo]n', text, re.IGNORECASE))
    has_nit_emisor = bool(re.search(r'Nit\s*Emisor', text, re.IGNORECASE))
    return sum([has_dte, has_autorizacion, has_nit_emisor]) >= 2


def encontrar_columnas(tables):
    """Finds the Total (Q) and Descripción column indices in the first 5 table rows."""
    total_col_idx = -1
    desc_col_idx = -1

    if tables:
        header_rows = tables[:min(5, len(tables))]
        for row_tbl in header_rows:
            if not row_tbl: continue
            for idx, cell in enumerate(row_tbl):
                if not cell: continue
                cell_norm = normalize_text(str(cell))
                if 'total' in cell_norm and 'descuento' not in cell_norm and '(q)' in cell_norm:
                    total_col_idx = idx
                if 'descripcion' in cell_norm:
                    desc_col_idx = idx
            if total_col_idx != -1 and desc_col_idx != -1:
                break

    # If we didn't find the description column, assume it's index 3
    if desc_col_idx == -1:
        desc_col_idx = 3

    return total_col_idx, desc_col_idx


def es_fila_de_producto(row_tbl, row_text_normalized):
    """Two-layer filter: admin keyword skip list + item number in first cells + enough content."""
    # FILTER 1: administrative keywords
    if any(keyword in row_text_normalized for keyword in SKIP_KEYWORDS):
        return False

    # FILTER 2: item number (1, 2, 3...) in one of the first 5 cells
    is_data_row = False
    for cell in row_tbl[:5]:
        if cell:
            if str(cell).strip().isdigit():
                is_data_row = True
                break
    if not is_data_row:
        return False

    # FILTER 3: "artifact rows" from white lines have very few non-empty cells
    non_empty_cells = sum(1 for c in row_tbl if c and str(c).strip())
    return non_empty_cells >= 3


def obtener_descripcion(row_tbl, desc_col_idx, row_text):
    """Description with the original fallback chain."""
    description = find_description_in_row(row_tbl)

    if not description:
        # Method 1: the detected description column
        if desc_col_idx != -1 and desc_col_idx < len(row_tbl):
            cell = row_tbl[desc_col_idx]
            if cell:
                description = str(cell).strip()

    if not description:
        # Method 2: index 3 (standard description column)
        if len(row_tbl) > 3 and row_tbl[3]:
            description = str(row_tbl[3]).strip()

    if not description:
        # Method 3: longest non-numeric cell
        longest = ""
        for cell in row_tbl:
            if not cell:
                continue
            cell_str = str(cell).strip()
            try:
                float(cell_str.replace(',', '.'))
                continue
            except ValueError:
                if len(cell_str) > len(longest) and cell_str.upper() not in ['BIEN', 'SERVICIO']:
                    longest = cell_str
        if longest:
            description = longest

    if not description:
        # Method 4: longest cell of any kind
        longest = max((str(c).strip() for c in row_tbl if c), key=len, default="")
        if longest:
            description = longest

    if not description:
        # Method 5: last resort
        description = "REVISAR: " + row_text[:50]

    return description


def extraer_lineas(tables):
    """Returns the product lines of a factura: [{'descripcion', 'texto_fila', 'total',
    'cantidad', 'precio_unitario', 'unidad', 'revisar'}, ...]"""
    total_col_idx, desc_col_idx = encontrar_columnas(tables)
    lineas = []

    for row_tbl in tables:
        if not row_tbl: continue

        row_text = " ".join([str(x) for x in row_tbl if x])
        if not es_fila_de_producto(row_tbl, normalize_text(row_text)):
            continue

        val = extract_value_from_row(row_tbl, total_col_idx)
        if val <= 0:
            continue

        descripcion = obtener_descripcion(row_tbl, desc_col_idx, row_text)
        cantidad, precio = extraer_cantidad_precio(row_tbl)
        lineas.append({
            'descripcion': descripcion,
            'texto_fila': row_text,   # classification matches on the full row text
            'total': val,
            'cantidad': cantidad,
            'precio_unitario': precio,
            'unidad': extraer_unidad(descripcion),
            'revisar': necesita_revision(cantidad, precio, val),
        })

    return lineas


def extraer_encabezado(text):
    """NITs, issuer name, school name, UUID, and date from the factura text."""
    nit_e_match = re.search(r'Emisor:\s*([0-9Kk\-]+)', text, re.I)
    nit_r_match = re.search(r'Receptor:\s*([0-9Kk\-]+)', text, re.I)
    name_e_match = re.search(r'(?:Factura(?:\s*Pequeño\s*Contribuyente)?)\s*\n+(.*?)\n+Nit\s*Emisor',
                             text, re.IGNORECASE | re.DOTALL)

    raw_name = re.sub(r'\s+', ' ', name_e_match.group(1).strip() if name_e_match else "N/A")
    name_e = re.split(r'(?i)n[úu]mero\s*de\s*autorizaci[óo]n', raw_name)[0]
    name_e = re.split(r'(?i)\bserie\b', name_e)[0].strip()

    return {
        'nit_emisor': nit_e_match.group(1).strip() if nit_e_match else "N/A",
        'nit_receptor': nit_r_match.group(1).strip() if nit_r_match else "N/A",
        'nombre_emisor': name_e,
        'nombre_escuela': extract_school_name(text),
        'autorizacion': extraer_autorizacion(text),
        'fecha': extraer_fecha(text),
    }


def procesar_pdf(archivo, nombre_archivo):
    """
    Main entry point. `archivo` is a path or file-like object (e.g. a Streamlit upload).
    Returns None for non-standard documents (proformas, cotizaciones), otherwise:
        {'archivo', 'dte', 'nit_emisor', 'nit_receptor', 'nombre_emisor',
         'nombre_escuela', 'autorizacion', 'fecha', 'lineas': [...see extraer_lineas]}
    """
    with pdfplumber.open(archivo) as pdf:
        text = "".join([p.extract_text() or "" for p in pdf.pages])
        tables = []
        for p in pdf.pages:
            t = p.extract_table()
            if t: tables.extend(t)

    tables = merge_split_rows(tables)

    if not es_factura_estandar(text):
        return None

    dte_m = re.search(r'N[úu]mero\s*de\s*DTE:\s*(\d+)', text, re.IGNORECASE)

    return {
        'archivo': nombre_archivo,
        'dte': dte_m.group(1) if dte_m else nombre_archivo,
        **extraer_encabezado(text),
        'lineas': extraer_lineas(tables),
    }
