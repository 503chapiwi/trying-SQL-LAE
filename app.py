import streamlit as st

st.set_page_config(page_title="MAGA – Facturas LAE", page_icon="🇬🇹")

pagina = st.navigation([
    st.Page("paginas/procesar.py", title="Procesar facturas", icon="📄", default=True),
    st.Page("paginas/base_de_datos.py", title="Base de datos", icon="📊"),
])
pagina.run()
