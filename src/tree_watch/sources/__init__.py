"""The two licence sources.

They differ in one way that must never be "simplified" away: the municipal PDF
comes out of pdfplumber in **visual** order and requires ``get_display``; the
Yeela API returns **logical** order and must not be passed through it. See the
module docstrings.
"""
