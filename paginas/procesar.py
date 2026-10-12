import streamlit as st

from config import lista_departamentos, lista_municipios, mapeos_excel
from extraccion import procesar_pdf
from clasificacion import clasificar_factura
from reporte_excel import ReporteMAGA, PlantillaInvalidaError
from db import conectar, crear_tablas, guardar_facturas

# --- TRUCO CSS PARA TRADUCIR LA INTERFAZ A ESPAÑOL ---
st.markdown("""
    <style>
        div[data-testid="stFileUploader"] label p {
            font-size: 40px !important;
        }
    </style>
""", unsafe_allow_html=True)

# --- WEB UI ---
st.title("🇬🇹 MAGA: Procesador de Facturas por la LAE")

selected_departamento = st.selectbox(
    label='1. Seleccione el Departamento',
    options=lista_departamentos(),
    index=None,
    placeholder="-- Seleccionar departamento --",
)

selected_municipio = st.selectbox(
    label='2. Seleccione el Municipio de las facturas',
    options=lista_municipios(selected_departamento) if selected_departamento else [],
    index=None,
    placeholder="-- Seleccionar municipio --",
    disabled=not selected_departamento,
    help="Todas las facturas que suba deben corresponder a este municipio"
)

uploaded_pdfs = st.file_uploader(label='3. Seleccione sus Facturas (PDFs)', type='pdf', accept_multiple_files=True)
uploaded_xlsx = st.file_uploader(label='4. Seleccione su Archivo de Excel', type='xlsx')

municipio_valido = bool(selected_departamento and selected_municipio)

if municipio_valido:
    st.info(f"📍 Municipio seleccionado: **{selected_municipio}, {selected_departamento}**. Asegúrese de que todas las facturas correspondan a este municipio.")
else:
    st.warning("⚠️ Por favor seleccione un departamento y un municipio antes de iniciar el proceso.")

if st.button("INICIAR PROCESO") and uploaded_pdfs and uploaded_xlsx and municipio_valido:
    try:
        user_m_name = selected_municipio

        # Validates the Excel template (columns + this municipality's row) before reading any PDFs
        reporte = ReporteMAGA(uploaded_xlsx.read(), mapeos_excel(selected_departamento),
                              user_m_name, selected_departamento)

        facturas_procesadas = []
        skipped_non_standard = []
        progress_bar = st.progress(0)

        for i, pdf_file in enumerate(uploaded_pdfs):
            factura = procesar_pdf(pdf_file, pdf_file.name)

            if factura is None:
                skipped_non_standard.append(pdf_file.name)
            else:
                clasificar_factura(factura)
                reporte.agregar_factura(factura, user_m_name, user_m_name)
                facturas_procesadas.append(factura)

            progress_bar.progress((i + 1) / len(uploaded_pdfs))

        xlsx_salida = reporte.finalizar()
        new_count = len(facturas_procesadas)
        unmatched_count = reporte.unmatched_count

        success_msg = f"¡Proceso completado! {new_count} facturas procesadas y agregadas al Excel con éxito."
        if unmatched_count > 0:
            success_msg += f"""\n\n⚠️ {unmatched_count} items sin clasificar encontrados. Están en la tercera hoja del archivo de Excel, 'Items sin Clasificar', para revisión manual.
                            Los totales de esos productos no fueron agregados a la cantidad de la primera hoja"""
        st.success(success_msg)

        # Save to the database. A failure here never blocks the Excel report.
        try:
            with conectar() as con:
                crear_tablas(con)
                resultado = guardar_facturas(con, facturas_procesadas, selected_departamento, user_m_name)
            db_msg = f"🗄️ {resultado['nuevas']} factura(s) guardadas en la base de datos."
            if resultado['duplicadas']:
                db_msg += f" {resultado['duplicadas']} ya estaban guardadas y no se duplicaron."
            st.info(db_msg)
            st.cache_data.clear()  # so the Base de datos page shows the new receipts right away
            if resultado['sin_uuid']:
                st.warning("⚠️ Estas facturas no tienen Número de Autorización legible y no se guardaron en la base de datos (sí están en el Excel):\n\n"
                           + "\n".join(f"- {a}" for a in resultado['sin_uuid']))
        except Exception as e:
            st.warning(f"⚠️ El reporte de Excel está listo, pero no se pudo guardar en la base de datos: {e}")

        if skipped_non_standard:
            warning_msg = f"⚠️ **{len(skipped_non_standard)} factura(s) no estándar fueron ignoradas** (proformas, cotizaciones, u otros formatos no oficiales). Estas deben procesarse manualmente:\n\n"
            for pdf_name in skipped_non_standard:
                warning_msg += f"- {pdf_name}\n"
            st.warning(warning_msg)

        st.download_button("Descargar Reporte Final", data=xlsx_salida,
                           file_name="Reporte_MAGA_Actualizado.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    except PlantillaInvalidaError as e:
        st.error(str(e))
    except Exception as e:
        st.error(f"Error crítico detectado: {e}")
