# Copyright (C) 2019-Today: Odoo Community Association (OCA)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).

from odoo import api, models, fields, _
from odoo.exceptions import ValidationError

class AccountInvoice(models.Model):
    _inherit = "account.invoice"

    delivery_ref_no = fields.Char(string='Delivery Reference No.', help="Delivery carrier reference number before"
                                                                        " the shipment is sent to the carrier.")

    @api.multi
    def action_match_einvoice_lines_picking(self):
        """
        Match invoice lines with picking lines
        :return: bool
        """
        for invoice in self:
            if not invoice.picking_ids:
                raise ValidationError(_('No picking linked to this invoice'))
            old_invoice_lines = invoice.invoice_line_ids.filtered(lambda l: not (l.name_xml or
                                                                                 l.SellersItemIdentification or
                                                                                 l.ManufacturersItemIdentification or
                                                                                 l.description))
            old_invoice_lines.unlink()
            for picking in invoice.picking_ids:
                moves = picking.mapped("move_lines")
                for move in moves:
                    partner_order_ref = move._get_partner_order_ref()
                    move_picking_ref = move._get_picking_ref()
                    invoice_line = invoice.invoice_line_ids.filtered(lambda l: l.product_id == move.product_id)
                    purchase_line = move.purchase_line_id
                    # Link Invoice Lines with Move and Purchase Line
                    invoice_line.write({
                        'purchase_id': picking.purchase_id.id,
                        'purchase_line_id': purchase_line.id,
                        'move_line_ids': [(6, 0, move.move_line_ids.ids)],
                        'partner_order_ref': partner_order_ref,
                        'moves_picking_ref': move_picking_ref,
                    })
                    # Link Purchase Line with Invoice Lines
                    if purchase_line:
                        purchase_line.write({
                            'invoice_lines': [(6, 0, invoice_line.ids)],
                        })
                    # Link Move with Invoice Lines
                    move.write({
                        'invoice_line_ids': [
                            (6, 0, invoice_line.ids)],
                    })
            invoice.compute_taxes()
            invoice._create_missing_supplierinfo()
        return True

    @api.multi
    def action_cancel(self):
        """
        Inherit to update related picking as '2binvoiced' when the invoice is
        cancelled (only for invoices, not refunds)
        :return: bool
        """
        result = super(AccountInvoice, self).action_cancel()
        pickings = self.filtered(
            lambda i: i.picking_ids and
                      i.type in ['out_invoice', 'in_invoice'] and
                      i.einvoice_state not in ['rejected', 'completed', 'waiting',
                                               'failed', False]).mapped("picking_ids")
        self.mapped("invoice_line_ids.move_line_ids")._set_as_2binvoiced()
        pickings._set_as_2binvoiced()
        return result

    @api.multi
    def action_invoice_draft(self):
        result = super(AccountInvoice, self).action_invoice_draft()
        pickings = self.filtered(
            lambda i: i.picking_ids and
            i.type in ['out_invoice', 'in_invoice']).mapped("picking_ids")
        self.mapped("invoice_line_ids.move_line_ids")._set_as_invoiced()
        pickings._set_as_invoiced()
        return result

    @api.multi
    def unlink(self):
        """
        Inherit the unlink to update related picking as "2binvoiced"
        (only for invoices, not refunds)
        :return:
        """
        pickings = self.filtered(
            lambda i: i.picking_ids and
            i.type in ['out_invoice', 'in_invoice']).mapped("picking_ids")
        self.mapped("invoice_line_ids.move_line_ids")._set_as_2binvoiced()
        pickings._set_as_2binvoiced()
        return super(AccountInvoice, self).unlink()

    @api.model
    def _prepare_refund(self, invoice, date_invoice=None, date=None,
                        description=None, journal_id=None):
        """
        Inherit to put link picking of the invoice into the new refund
        :param invoice: self recordset
        :param date_invoice: str
        :param date: str
        :param description: str
        :param journal_id: int
        :return: dict
        """
        result = super(AccountInvoice, self)._prepare_refund(
            invoice=invoice, date_invoice=date_invoice, date=date,
            description=description, journal_id=journal_id)
        result.update({
            'picking_ids': [(6, False, invoice.picking_ids.ids)],
        })
        return result


class AccountInvoiceLine(models.Model):
    _inherit = 'account.invoice.line'

    lot_ids = fields.Many2many('stock.production.lot', relation='account_invoice_line_stock_lot_rel',
                               column1='invoice_line_id', column2='lot_id',
                               string='Lots/Serial Numbers')
    moves_picking_ref = fields.Char(string='Picking Ref')
    partner_order_ref = fields.Char(string='Order Reference')
    purchase_line_amount = fields.Float(string='PO Unit', related='purchase_line_id.price_unit')
