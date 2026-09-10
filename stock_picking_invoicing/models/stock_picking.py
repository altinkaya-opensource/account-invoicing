# Copyright (C) 2019-Today: Odoo Community Association (OCA)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).

from odoo import api, models
from datetime import datetime


class StockPicking(models.Model):
    _name = "stock.picking"
    _inherit = [
        _name,
        "stock.invoice.state.mixin",
    ]

    def set_to_be_invoiced(self):
        """
        Button to set Invoice State to To Be Invoice.
        """
        self._set_as_2binvoiced()
        self.mapped("move_ids")._set_as_2binvoiced()

    def set_as_invoiced(self):
        """
        Button to set Invoice State to Invoiced.
        """
        self._set_as_invoiced()
        # A cancelled move was never invoiced. Stamping it would claim on the
        # picking form that a line which shipped nothing had been billed.
        self.mapped("move_ids").filtered(
            lambda m: m.state != "cancel"
        )._set_as_invoiced()

    def set_as_not_billable(self):
        """
        Button to set Invoice State to Not Billable.
        """
        self._set_as_not_billable()
        self.mapped("move_ids")._set_as_not_billable()

    def _get_partner_to_invoice(self):
        self.ensure_one()
        return self.sale_id.partner_invoice_id

    def action_assign(self):
        """If any stock move is to be invoiced, picking status is updated"""
        if any(m.invoice_state == "2binvoiced" for m in self.mapped("move_ids")):
            self.write({"invoice_state": "2binvoiced"})
        return super().action_assign()

    @api.onchange("invoice_state")
    def _onchange_invoice_state(self):
        for record in self:
            record._update_invoice_state(record.invoice_state)
            record.mapped("move_ids")._update_invoice_state(record.invoice_state)

    def button_validate(self):
        for picking in self.filtered(
            lambda p: p.picking_type_id.code not in ["internal", "mrp_operation"]
        ):
            picking.set_to_be_invoiced()
        return super().button_validate()

    def button_create_fast_invoice(self):
        """
        Quick create invoice for this picking. Skip quick create wizard
        if the picking is created from a purchase order.
        """
        self.ensure_one()
        if self.sale_id:
            self = self.with_context(active_id=self.id, active_ids=[self.id])
            wizard = self.env["stock.invoice.onshipping"].create(
                {
                    "invoice_date": datetime.today().date(),
                    "sale_journal": 1,
                }
            )
            return wizard.action_generate()
        else:
            return self.env.ref(
                "stock_picking_invoicing.action_stock_invoice_onshipping"
            ).read()[0]
