"""
Render an invoice payload (from invoicing.build_* ) to a one-page PDF with
reportlab -- no external binaries, works headless on Windows. Mirrors the
ForestKind / Illukkumbura -> ODEL tax-invoice template.

    from invoice_pdf import render_pdf
    render_pdf(inv_dict, "FKB00003_ISI02343.pdf")
"""

from __future__ import annotations

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle, Paragraph,
                                Spacer, HRFlowable)

INK = colors.HexColor("#2A2233")
GREEN = colors.HexColor("#2C5A1E")
MUTED = colors.HexColor("#7C736A")
LINE = colors.HexColor("#D9CFBF")
HEADBG = colors.HexColor("#EFE9DD")

_P = ParagraphStyle("p", fontName="Helvetica", fontSize=8.7, leading=12, textColor=INK)
_PR = ParagraphStyle("pr", parent=_P, alignment=TA_RIGHT)
_LBL = ParagraphStyle("lbl", parent=_P, textColor=MUTED, fontSize=8)
_TITLE = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=20, leading=22,
                        alignment=TA_CENTER, textColor=GREEN, spaceAfter=2)
_H = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=8.7, leading=12, textColor=INK)
_SMALL = ParagraphStyle("s", parent=_P, fontSize=7.6, textColor=MUTED, leading=10)


def _money(v):
    return f"{float(v or 0):,.2f}"


def render_pdf(inv: dict, out_path: str) -> str:
    n = inv["numbers"]
    sup, cus, bank, so = inv["supplier"], inv["customer"], inv["bank"], inv["signoff"]
    doc = SimpleDocTemplate(out_path, pagesize=A4,
                            leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"{n['fkb']} {n['isi']}")
    W = doc.width
    story = [Paragraph("TAX INVOICE", _TITLE),
             HRFlowable(width="100%", thickness=1.1, color=GREEN, spaceAfter=8)]

    # Invoice date / tax number
    meta = Table([[Paragraph(f'<font color="#7C736A">Invoice Date:</font> <b>{inv["invoice_date"]}</b>', _P),
                   Paragraph(f'<font color="#7C736A">Tax Invoice Number:</font> <b>{n["tax_invoice_number"]}</b>', _PR)]],
                 colWidths=[W * 0.5, W * 0.5])
    meta.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [meta, Spacer(1, 7)]

    # Parties
    sup_block = Paragraph(
        f'<font color="#7C736A">Supplier\'s TIN Number:</font> {sup["tin"]}<br/>'
        f'<font color="#7C736A">Supplier\'s Name:</font> <b>{sup["name"]}</b><br/>'
        f'<font color="#7C736A">Supplier\'s Address:</font> ' + "<br/>".join(sup["address_lines"]) +
        f'<br/>{sup.get("email","")}<br/>'
        f'<font color="#7C736A">Phone:</font> {sup.get("phone","")}', _P)
    cus_block = Paragraph(
        f'<font color="#7C736A">Customer\'s TIN Number:</font> {cus["tin"]}<br/>'
        f'<font color="#7C736A">Customer\'s Name:</font> <b>{cus["name"]}</b><br/>'
        f'<font color="#7C736A">Customer\'s Address:</font> ' + "<br/>".join(cus["address_lines"]), _P)
    parties = Table([[sup_block, cus_block]], colWidths=[W * 0.52, W * 0.48])
    parties.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                 ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                                 ("TOPPADDING", (0, 0), (-1, -1), 2),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    story += [parties, Spacer(1, 7)]

    # Delivery / place of supply
    pos = inv["customer"].get("place_of_supply") or [cus["name"]]
    deliv = Table([[Paragraph(f'<font color="#7C736A">Delivery / Supply:</font> <b>{inv.get("delivery_date","")}</b>', _P),
                    Paragraph('<font color="#7C736A">Place of Supply:</font> ' + "<br/>".join(pos), _PR)]],
                  colWidths=[W * 0.5, W * 0.5])
    deliv.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [deliv, Spacer(1, 9)]

    # Line items
    head = [Paragraph('<b>DESCRIPTION</b>', _H), Paragraph('<b>QTY</b>', ParagraphStyle("hc", parent=_H, alignment=TA_RIGHT)),
            Paragraph('<b>UNIT PRICE</b>', ParagraphStyle("hr", parent=_H, alignment=TA_RIGHT)),
            Paragraph('<b>TOTAL</b>', ParagraphStyle("hr2", parent=_H, alignment=TA_RIGHT))]
    data = [head]
    for l in inv["lines"]:
        data.append([Paragraph(l["description"] or l["sku"], _P),
                     Paragraph(f'{int(round(l["qty"]))}', _PR),
                     Paragraph(_money(l["unit_price"]), _PR),
                     Paragraph(_money(l["total"]), _PR)])
    tbl = Table(data, colWidths=[W * 0.58, W * 0.10, W * 0.16, W * 0.16], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HEADBG),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, GREEN),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [tbl, Spacer(1, 8)]

    # Totals (right) beside remarks/bank (left)
    tot_rows = [["Subtotal", _money(inv["subtotal"])],
                ["Discount", _money(inv["discount"])],
                ["Subtotal less discount", _money(inv["subtotal_less_discount"])],
                [f'VAT {int(round(inv["vat_rate"]*100))}%', _money(inv["vat"])],
                ["TOTAL INCL. VAT", _money(inv["total"])]]
    tot = Table([[Paragraph(r[0], _LBL if i < 4 else _H),
                  Paragraph(r[1], _PR if i < 4 else ParagraphStyle("b", parent=_PR, fontName="Helvetica-Bold"))]
                 for i, r in enumerate(tot_rows)],
                colWidths=[W * 0.26, W * 0.16])
    tot.setStyle(TableStyle([("LINEABOVE", (0, 4), (-1, 4), 0.8, GREEN),
                             ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                             ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    remarks = Paragraph(
        '<b>Remarks / Payment Instructions</b><br/>'
        f'{bank.get("method","CEFT")} payments made out to:<br/>{bank.get("payee","")}<br/>'
        f'Bank - {bank.get("bank","")}<br/>Branch - {bank.get("branch","")}<br/>'
        f'Acc No - {bank.get("acc_no","")}', _SMALL)
    pair = Table([[remarks, tot]], colWidths=[W * 0.56, W * 0.44])
    pair.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [pair, Spacer(1, 10)]

    # Amount in words + method
    story += [Paragraph(f'<font color="#7C736A">Total amount in words:</font> <b>{inv["total_in_words"]}</b>', _P),
              Paragraph(f'<font color="#7C736A">Payment method:</font> {bank.get("method","CEFT")}', _SMALL),
              Spacer(1, 16)]

    # Sign-off
    sign = Table([[Paragraph(f'<b>{so.get("invoiced_by","")}</b><br/><font color="#7C736A" size="7">Invoiced by</font>', _P),
                   Paragraph(f'<b>{so.get("checked_by","")}</b><br/><font color="#7C736A" size="7">Checked by</font>', _P),
                   Paragraph(f'<b>{so.get("accepted_by","")}</b><br/><font color="#7C736A" size="7">Accepted by / Seal &amp; Signature</font>', _P)]],
                 colWidths=[W / 3.0] * 3)
    sign.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                              ("LINEABOVE", (0, 0), (-1, 0), 0.4, LINE),
                              ("TOPPADDING", (0, 0), (-1, -1), 18),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [sign, Spacer(1, 10),
              Paragraph(f'<font color="#7C736A" size="7.5">INV {n["footer_ref"]} &nbsp;·&nbsp; '
                        f'Amounts in {inv.get("currency","LKR")}</font>', _SMALL)]

    doc.build(story)
    return out_path
