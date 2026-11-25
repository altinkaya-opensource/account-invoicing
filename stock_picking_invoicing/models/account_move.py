# Copyright (C) 2019-Today: Odoo Community Association (OCA)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).

from odoo import Command, _, models
from odoo.exceptions import UserError
from odoo.tools import float_compare


class AccountMove(models.Model):
    _inherit = "account.move"

    def button_cancel(self):
        """
        Inherit to update related picking as '2binvoiced' when the invoice is
        cancelled (only for invoices, not refunds)
        :return: bool
        """
        result = super().button_cancel()
        pickings = self.filtered(
            lambda i: i.picking_ids
            and i.move_type in ["out_invoice", "in_invoice"]
            and i.einvoice_state
            not in ["rejected", "completed", "waiting", "failed", False]
        ).mapped("picking_ids")
        self.mapped("invoice_line_ids.move_line_ids")._set_as_2binvoiced()
        pickings._set_as_2binvoiced()
        return result

    def button_draft(self):
        result = super().button_draft()
        pickings = self.filtered(
            lambda i: i.picking_ids and i.move_type in ["out_invoice", "in_invoice"]
        ).mapped("picking_ids")
        self.mapped("invoice_line_ids.move_line_ids")._set_as_invoiced()
        pickings._set_as_invoiced()
        return result

    def unlink(self):
        """
        Inherit the unlink to update related picking as "2binvoiced"
        (only for invoices, not refunds)
        :return:
        """
        pickings = self.filtered(
            lambda i: i.picking_ids and i.move_type in ["out_invoice", "in_invoice"]
        ).mapped("picking_ids")
        self.mapped("invoice_line_ids.move_line_ids")._set_as_2binvoiced()
        pickings._set_as_2binvoiced()
        return super().unlink()

    def _reverse_moves(self, default_values_list=None, cancel=False):
        reverse_moves = super()._reverse_moves(
            default_values_list=default_values_list, cancel=cancel
        )
        for move, reverse_move in zip(self, reverse_moves):
            for line in move.invoice_line_ids:
                reverse_line = reverse_move.invoice_line_ids.filtered(
                    lambda l: l.product_id == line.product_id
                )
                reverse_line.move_line_ids = line.move_line_ids.ids

        return reverse_moves

    # Linking supplier receipts to a bill that already has its lines

    def _match_supplier_receipts(self, pickings):
        """Pair the unbilled received moves of ``pickings`` with this bill's
        lines, per product. Nothing is written.

        One dict per product: its ``moves``, the bill ``lines`` of that product
        a receipt does not cover yet, the ``received`` and ``billed``
        quantities in the product's unit, and ``exact`` when the two are
        equal. Sums are compared, never single moves: a receipt splits a
        product over several moves (an over-delivery is a move of its own)
        and a bill may repeat a product.
        """
        self.ensure_one()
        moves = pickings.move_ids._unbilled_supplier_receipts()
        matches = []
        for product in moves.product_id:
            rounding = product.uom_id.rounding
            lines = self.invoice_line_ids.filtered(
                lambda line: line.display_type == "product"
                and line.product_id == product
                and float_compare(
                    line._unreceived_quantity(), 0.0, precision_rounding=rounding
                )
                > 0
            )
            product_moves = moves.filtered(lambda move: move.product_id == product)
            received = product_moves._received_quantity(product.uom_id)
            billed = sum(line._unreceived_quantity() for line in lines)
            matches.append(
                {
                    "product": product,
                    "moves": product_moves,
                    "lines": lines,
                    "received": received,
                    "billed": billed,
                    "exact": bool(lines)
                    and float_compare(received, billed, precision_rounding=rounding)
                    == 0,
                }
            )
        return matches

    def _link_supplier_receipts(self, links):
        """Link received stock moves to this bill's lines.

        ``links`` is a list of (bill lines, stock moves) pairs. Only the link
        is made: the bill keeps its lines and quantities, and the receipts
        theirs. A line without a purchase line takes the one its moves share,
        the moves become invoiced, and so does a picking once none of its
        received moves is left unlinked.
        """
        self.ensure_one()
        if self.move_type != "in_invoice" or self.state == "cancel":
            raise UserError(
                _("Receipts are linked to a vendor bill that is not cancelled.")
            )
        linked = self.env["stock.move"]
        for lines, moves in links:
            self._check_supplier_receipt_link(lines, moves)
            lines.write({"move_line_ids": [Command.link(move.id) for move in moves]})
            purchase_line = moves.purchase_line_id
            if len(purchase_line) == 1:
                lines.filtered(
                    lambda line: not line.purchase_line_id
                ).purchase_line_id = purchase_line
            linked |= moves
        linked._set_as_invoiced()
        linked.picking_id.filtered(
            lambda picking: not picking.move_ids._unbilled_supplier_receipts()
        )._set_as_invoiced()
        return linked

    def _check_supplier_receipt_link(self, lines, moves):
        """Refuse a link the bill, its lines or the moves cannot take."""
        if (
            not lines
            or not moves
            or lines.move_id != self
            or any(line.display_type != "product" for line in lines)
        ):
            raise UserError(
                _(
                    "Receipts are linked to product lines of the bill %s.",
                    self.display_name,
                )
            )
        if moves != moves._unbilled_supplier_receipts():
            raise UserError(
                _(
                    "Only received supplier moves without a bill can be linked: %s",
                    ", ".join(moves.mapped("display_name")),
                )
            )
        if any(
            picking.partner_id.commercial_partner_id != self.commercial_partner_id
            for picking in moves.picking_id
        ):
            raise UserError(
                _(
                    "The receipts %(receipts)s are not from %(supplier)s.",
                    receipts=", ".join(moves.picking_id.mapped("name")),
                    supplier=self.commercial_partner_id.display_name,
                )
            )
        # One product per link: a line without a product is only reached by
        # naming it, and one with a product never takes another's receipt.
        if len(moves.product_id) != 1 or lines.product_id - moves.product_id:
            raise UserError(
                _(
                    "The bill lines are for %(billed)s, the receipt for %(received)s.",
                    billed=", ".join(lines.product_id.mapped("display_name")),
                    received=", ".join(moves.product_id.mapped("display_name")),
                )
            )

    def _unlink_supplier_receipts(self, moves):
        """Undo ``_link_supplier_receipts`` for ``moves``.

        The purchase line of a bill line left without a receipt is cleared.
        """
        self.ensure_one()
        lines = self.invoice_line_ids.filtered(lambda line: line.move_line_ids & moves)
        lines.write({"move_line_ids": [Command.unlink(move.id) for move in moves]})
        lines.filtered(lambda line: not line.move_line_ids).purchase_line_id = False
        moves = moves._unbilled_supplier_receipts()
        moves._set_as_2binvoiced()
        moves.picking_id._set_as_2binvoiced()


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    def _unreceived_quantity(self):
        """The quantity of the line no linked receipt covers, in the product's
        unit (the line's own unit when it has no product)."""
        self.ensure_one()
        uom = self.product_id.uom_id or self.product_uom_id
        billed = self.product_uom_id._compute_quantity(
            self.quantity, uom, raise_if_failure=False
        )
        return billed - self.move_line_ids._received_quantity(uom)
