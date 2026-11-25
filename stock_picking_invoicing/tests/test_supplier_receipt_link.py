# Copyright 2026 Ahmet Yiğit Budak (https://github.com/yibudak)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).
"""Linking supplier receipts to a vendor bill that already has its lines."""

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import new_test_user, tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestSupplierReceiptLink(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.supplier = cls.env["res.partner"].create({"name": "Receipt Link Supplier"})
        cls.product, cls.other = cls.env["product.product"].create(
            [
                {"name": "Receipt Link Screw", "type": "product"},
                {"name": "Receipt Link Nut", "type": "product"},
            ]
        )
        cls.picking_type = cls.env["stock.picking.type"].search(
            [
                ("code", "=", "incoming"),
                ("warehouse_id.company_id", "=", cls.env.company.id),
            ],
            limit=1,
        )

    def _receive(self, quantities, partner=None):
        """A done receipt with one move per (product, quantity), each on its
        own purchase order line unless the product repeats."""
        partner = partner or self.supplier
        order = self.env["purchase.order"].create({"partner_id": partner.id})
        order_lines = {}
        moves = []
        for product, quantity in quantities:
            if product not in order_lines:
                order_lines[product] = self.env["purchase.order.line"].create(
                    {
                        "order_id": order.id,
                        "product_id": product.id,
                        "product_qty": quantity,
                        "price_unit": 10.0,
                    }
                )
            moves.append(
                Command.create(
                    {
                        "name": product.name,
                        "product_id": product.id,
                        "product_uom_qty": quantity,
                        "product_uom": product.uom_id.id,
                        "purchase_line_id": order_lines[product].id,
                        "location_id": self.env.ref(
                            "stock.stock_location_suppliers"
                        ).id,
                        "location_dest_id": (
                            self.picking_type.default_location_dest_id.id
                        ),
                    }
                )
            )
        picking = self.env["stock.picking"].create(
            {
                "partner_id": partner.id,
                "picking_type_id": self.picking_type.id,
                "location_id": self.env.ref("stock.stock_location_suppliers").id,
                "location_dest_id": self.picking_type.default_location_dest_id.id,
                "move_ids": moves,
            }
        )
        picking.action_confirm()
        for move in picking.move_ids:
            move.quantity_done = move.product_uom_qty
        # button_validate stamps the receipt before it runs the transfer.
        picking.set_to_be_invoiced()
        picking._action_done()
        return picking

    def _bill(self, quantities):
        return self.env["account.move"].create(
            {
                "move_type": "in_invoice",
                "partner_id": self.supplier.id,
                "invoice_date": fields.Date.today(),
                "invoice_line_ids": [
                    Command.create(
                        {
                            "product_id": product.id,
                            "quantity": quantity,
                            "price_unit": 10.0,
                        }
                    )
                    for product, quantity in quantities
                ],
            }
        )

    def test_equal_sums_link_without_changing_the_bill(self):
        """Two moves of one product are compared with the bill line as a sum."""
        picking = self._receive([(self.product, 3000.0), (self.product, 200.0)])
        bill = self._bill([(self.product, 3200.0)])
        (match,) = bill._match_supplier_receipts(picking)
        self.assertTrue(match["exact"])
        self.assertEqual(match["received"], 3200.0)

        bill._link_supplier_receipts([(match["lines"], match["moves"])])

        line = bill.invoice_line_ids
        self.assertEqual(len(line), 1)
        self.assertEqual(line.quantity, 3200.0)
        self.assertEqual(line.move_line_ids, picking.move_ids)
        self.assertEqual(bill.picking_ids, picking)
        self.assertEqual(picking.invoice_state, "invoiced")
        self.assertEqual(line.purchase_line_id, picking.move_ids.purchase_line_id)
        self.assertEqual(line.purchase_line_id.qty_invoiced, 3200.0)
        self.assertFalse(bill._match_supplier_receipts(picking))

        bill._unlink_supplier_receipts(picking.move_ids)
        self.assertFalse(bill.picking_ids)
        self.assertFalse(line.purchase_line_id)
        self.assertEqual(picking.invoice_state, "2binvoiced")
        self.assertEqual(set(picking.move_ids.mapped("invoice_state")), {"2binvoiced"})

    def test_wizard_links_equal_products_and_leaves_the_rest(self):
        """The wizard adds no bill line; a quantity difference stays unlinked
        until the pair is named."""
        picking = self._receive([(self.product, 10.0), (self.other, 5.0)])
        bill = self._bill([(self.product, 10.0), (self.other, 7.0)])
        wizard = (
            self.env["stock.invoice.onshipping"]
            .with_context(active_ids=picking.ids, active_model="stock.picking")
            .create({"connect_to_einvoice": True, "supplier_invoice_id": bill.id})
        )
        wizard.action_generate()

        self.assertEqual(bill.invoice_line_ids.mapped("quantity"), [10.0, 7.0])
        screw_line, nut_line = bill.invoice_line_ids
        screw_move = picking.move_ids.filtered(lambda m: m.product_id == self.product)
        nut_move = picking.move_ids - screw_move
        self.assertEqual(screw_line.move_line_ids, screw_move)
        self.assertEqual(screw_move.invoice_state, "invoiced")
        self.assertFalse(nut_line.move_line_ids)
        self.assertEqual(nut_move.invoice_state, "2binvoiced")
        self.assertEqual(picking.invoice_state, "2binvoiced")
        self.assertIn(self.other.display_name, bill.message_ids[0].body)
        # Linked in part, the receipt stays on the list of unbilled receipts.
        unbilled = [("unbilled_receipt", "=", True), ("id", "in", picking.ids)]
        self.assertEqual(self.env["stock.picking"].search(unbilled), picking)
        self.assertTrue(picking.unbilled_receipt)

        bill._link_supplier_receipts([(nut_line, nut_move)])
        self.assertEqual(nut_line.quantity, 7.0)
        self.assertEqual(picking.invoice_state, "invoiced")
        self.assertFalse(self.env["stock.picking"].search(unbilled))

        # A cancelled bill no longer bills the receipt.
        bill.button_cancel()
        self.assertEqual(self.env["stock.picking"].search(unbilled), picking)

    def test_wizard_refuses_when_nothing_is_equal(self):
        picking = self._receive([(self.product, 10.0)])
        bill = self._bill([(self.product, 12.0)])
        wizard = (
            self.env["stock.invoice.onshipping"]
            .with_context(active_ids=picking.ids, active_model="stock.picking")
            .create({"connect_to_einvoice": True, "supplier_invoice_id": bill.id})
        )
        with self.assertRaises(UserError):
            wizard.action_generate()
        self.assertFalse(bill.picking_ids)

    def test_a_later_receipt_takes_what_the_line_has_left(self):
        """One bill line covers two receipts linked one after the other."""
        first = self._receive([(self.product, 100.0)])
        second = self._receive([(self.product, 20.0)])
        bill = self._bill([(self.product, 120.0)])
        line = bill.invoice_line_ids
        (match,) = bill._match_supplier_receipts(first)
        self.assertFalse(match["exact"])
        bill._link_supplier_receipts([(line, first.move_ids)])

        (match,) = bill._match_supplier_receipts(second)
        self.assertTrue(match["exact"])
        bill._link_supplier_receipts([(match["lines"], match["moves"])])
        self.assertEqual(bill.picking_ids, first | second)

    def test_wrong_links_are_refused(self):
        picking = self._receive([(self.product, 10.0)])
        bill = self._bill([(self.other, 10.0)])
        with self.assertRaises(UserError):
            bill._link_supplier_receipts([(bill.invoice_line_ids, picking.move_ids)])
        # Two products in one link would cross-link them.
        mixed = self._receive([(self.product, 10.0), (self.other, 10.0)])
        both = self._bill([(self.product, 10.0), (self.other, 10.0)])
        with self.assertRaises(UserError):
            both._link_supplier_receipts([(both.invoice_line_ids, mixed.move_ids)])
        stranger = self.env["res.partner"].create({"name": "Another Supplier"})
        foreign = self._receive([(self.other, 10.0)], partner=stranger)
        with self.assertRaises(UserError):
            bill._link_supplier_receipts([(bill.invoice_line_ids, foreign.move_ids)])
        bill.button_cancel()
        own = self._receive([(self.other, 10.0)])
        with self.assertRaises(UserError):
            bill._link_supplier_receipts([(bill.invoice_line_ids, own.move_ids)])

    def test_a_billing_user_can_link_a_posted_bill(self):
        picking = self._receive([(self.product, 10.0)])
        bill = self._bill([(self.product, 10.0)])
        bill.action_post()
        user = new_test_user(
            self.env,
            login="receipt-link-billing",
            password="Receipt-Link-2026",
            groups="account.group_account_invoice",
        )
        bill = bill.with_user(user)
        bill._link_supplier_receipts([(bill.invoice_line_ids, picking.move_ids)])
        self.assertEqual(bill.picking_ids, picking)
