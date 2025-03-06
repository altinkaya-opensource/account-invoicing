# Copyright (C) 2019-Today: Odoo Community Association (OCA)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).

from odoo import fields, models
from odoo.tools.float_utils import float_round


class StockMove(models.Model):
    _name = "stock.move"
    _inherit = [
        _name,
        "stock.invoice.state.mixin",
    ]

    def _get_taxes(self, fiscal_position, inv_type):
        """
        Map product taxes based on given fiscal position
        :param fiscal_position: account.fiscal.position recordset
        :param inv_type: string
        :return: account.tax recordset
        """
        product = self.mapped("product_id")
        product.ensure_one()
        if inv_type in ("out_invoice", "out_refund"):
            taxes = product.taxes_id
        else:
            taxes = product.supplier_taxes_id
        company_id = self.env.context.get("force_company", self.env.company.id)
        my_taxes = taxes.filtered(lambda r: r.company_id.id == company_id)
        return fiscal_position.map_tax(my_taxes)

    def _get_account(self, fiscal_position, account):
        """
        Map the given account with given fiscal position
        :param fiscal_position: account.fiscal.position recordset
        :param account: account.account recordset
        :return: account.account recordset
        """
        return fiscal_position.map_account(account)

    def _get_partner_order_ref(self):
        """
        Gets partner order reference
        :return: string
        """
        count = 0
        if self.sale_line_id:
            order_id = self.sale_line_id.order_id
            for line in order_id.order_line:
                count += 1
                if line == self.sale_line_id:
                    return f"{order_id.client_order_ref or order_id.name}-{str(count)}"

        elif self.purchase_line_id:
            purchase_id = self.purchase_line_id.order_id
            for line in purchase_id.order_line:
                count += 1
                if line == self.purchase_line_id:
                    return f"{purchase_id.name}-{str(count)}"

        else:
            return self.picking_id.name

    def _get_picking_ref(self):
        """
        Gets picking reference
        :return: string
        """
        return self.picking_id.document_number or self.picking_id.name

    def _get_price_unit_invoice(self):
        """
        Gets price unit for invoice
        :return: float
        """

        product = self.mapped("product_id")
        product.ensure_one()

        if self.sale_line_id:
            return self.sale_line_id.price_unit
        if self.purchase_line_id:
            return self.purchase_line_id.price_unit
        else:
            return 0.0

    def _prepare_extra_move_vals(self, qty):
        """Copy invoice state for a new extra stock move"""
        values = super()._prepare_extra_move_vals(qty)
        values["invoice_state"] = self.invoice_state
        return values

    def _prepare_move_split_vals(self, uom_qty):
        """Copy invoice state for a new splitted stock move"""
        values = super()._prepare_move_split_vals(uom_qty)
        values["invoice_state"] = self.invoice_state
        return values
