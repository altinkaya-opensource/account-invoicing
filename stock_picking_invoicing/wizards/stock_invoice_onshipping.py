# Copyright (C) 2019-Today: Odoo Community Association (OCA)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).

from odoo import _, api, fields, models
from odoo.exceptions import UserError

JOURNAL_TYPE_MAP = {
    ("outgoing", "customer"): ["sale"],
    ("outgoing", "supplier"): ["purchase"],
    ("outgoing", "transit"): ["sale", "purchase"],
    ("incoming", "supplier"): ["purchase"],
    ("incoming", "customer"): ["sale"],
    ("incoming", "transit"): ["purchase", "sale"],
}

INVOICE_TYPE_MAP = {
    # Picking Type Code | Local Origin Usage | Local Dest Usage
    ("outgoing", "internal", "customer"): "out_invoice",
    ("incoming", "customer", "internal"): "out_refund",
    ("incoming", "customer", "customer"): "out_refund",
    ("incoming", "supplier", "internal"): "in_invoice",
    ("outgoing", "internal", "supplier"): "in_refund",
    ("outgoing", "internal", "internal"): "in_refund",
    ("incoming", "transit", "internal"): "in_invoice",
    ("outgoing", "transit", "supplier"): "in_refund",
    ("outgoing", "transit", "customer"): "out_invoice",
}


class StockInvoiceOnshipping(models.TransientModel):
    _name = "stock.invoice.onshipping"
    _description = "Stock Invoice Onshipping"

    @api.model
    def _get_journal_type(self):
        active_ids = self.env.context.get("active_ids", [])
        if active_ids:
            active_ids = active_ids[0]
        if self.env.context.get("active_model") == "stock.ewaybill":
            picking = self.env["stock.ewaybill"].browse(active_ids).picking_ids
        else:
            pick_obj = self.env["stock.picking"]
            picking = pick_obj.browse(active_ids)
        if not picking or not picking.move_ids:
            return "sale"
        pick_type_code = picking.picking_type_id.code
        line = fields.first(picking.move_ids)
        if pick_type_code == "incoming":
            usage = line.location_id.usage
        else:
            usage = line.location_dest_id.usage
        return JOURNAL_TYPE_MAP.get((pick_type_code, usage), ["sale"])[0]

    journal_type = fields.Selection(
        selection=[
            ("purchase", "Create Supplier Invoice"),
            ("sale", "Create Customer Invoice"),
        ],
        default=_get_journal_type,
        readonly=True,
    )
    group = fields.Selection(
        selection=[
            ("picking", "Picking"),
            ("partner", "Partner"),
            ("partner_product", "Partner/Product"),
        ],
        default="partner",
        help="Group pickings/moves to create invoice(s):\n"
        "Picking: One invoice per picking;\n"
        "Partner: One invoice for each picking's partner;\n"
        "Partner/Product: One invoice per picking's partner and group "
        "product into a single invoice line.",
        required=True,
    )
    invoice_date = fields.Date()
    sale_journal = fields.Many2one(
        comodel_name="account.journal",
        domain="[('type', '=', 'sale')]",
        default=lambda self: self._default_journal("sale"),
        ondelete="cascade",
    )
    purchase_journal = fields.Many2one(
        comodel_name="account.journal",
        domain="[('type', '=', 'purchase')]",
        default=lambda self: self._default_journal("purchase"),
        ondelete="cascade",
    )
    show_sale_journal = fields.Boolean()
    show_purchase_journal = fields.Boolean()
    connect_to_einvoice = fields.Boolean(string="Connect to e-invoice")
    partner_id = fields.Many2one("res.partner", string="Partner", readonly=True)
    supplier_invoice_id = fields.Many2one("account.move", string="Supplier Invoice")

    @api.model
    def default_get(self, fields_list):
        """
        Inherit to add default invoice_date
        :param fields_list: list of str
        :return: dict
        """
        result = super().default_get(fields_list)
        result.update({"invoice_date": fields.Date.today()})
        pickings = self._load_pickings()
        if pickings and "customer" not in pickings.mapped("location_dest_id.usage"):
            partner_id = pickings.mapped("partner_id")
            if len(partner_id) > 1:
                raise UserError(_("You can only invoice one partner"))
            result.update({"partner_id": partner_id.id})

        return result

    @api.onchange("group")
    def onchange_group(self):
        self.ensure_one()
        (
            sale_pickings,
            sale_refund_pickings,
            purchase_pickings,
            purchase_refund_pickings,
        ) = self.get_split_pickings()
        self.show_sale_journal = bool(sale_pickings)
        self.show_purchase_journal = bool(purchase_pickings)

    def get_partner_sum(self, pickings, partner, inv_type, picking_type, usage):
        pickings = pickings.filtered(
            lambda x: x.picking_type_id.code == picking_type and x.partner_id == partner
        )
        lines = pickings.mapped("move_ids")
        if picking_type == "outgoing":
            moves = lines.filtered(lambda x: x.location_dest_id.usage == usage)
        else:
            moves = lines.filtered(lambda x: x.location_id.usage == usage)
        total = sum((m._get_price_unit_invoice() * m.product_uom_qty) for m in moves)
        return total, moves.mapped("picking_id")

    def get_split_pickings(self):
        self.ensure_one()
        picking_obj = self.env["stock.picking"]
        pickings = picking_obj.browse(self.env.context.get("active_ids", []))
        if self.group != "picking":
            return self.get_split_pickings_grouped(pickings)
        return self.get_split_pickings_nogrouped(pickings)

    def get_split_pickings_grouped(self, pickings):
        sale_pickings = self.env["stock.picking"].browse()
        sale_refund_pickings = self.env["stock.picking"].browse()
        purchase_pickings = self.env["stock.picking"].browse()
        purchase_refund_pickings = self.env["stock.picking"].browse()

        for partner in pickings.mapped("partner_id"):
            so_sum, so_pickings = self.get_partner_sum(
                pickings, partner, "out_invoice", "outgoing", "customer"
            )
            si_sum, si_pickings = self.get_partner_sum(
                pickings, partner, "out_invoice", "incoming", "customer"
            )
            if (so_sum - si_sum) >= 0:
                sale_pickings |= so_pickings | si_pickings
            else:
                sale_refund_pickings |= so_pickings | si_pickings
            pi_sum, pi_pickings = self.get_partner_sum(
                pickings, partner, "in_invoice", "incoming", "supplier"
            )
            po_sum, po_pickings = self.get_partner_sum(
                pickings, partner, "in_invoice", "outgoing", "supplier"
            )
            if (pi_sum - po_sum) >= 0:
                purchase_pickings |= pi_pickings | po_pickings
            else:
                purchase_refund_pickings |= pi_pickings | po_pickings

        return (
            sale_pickings,
            sale_refund_pickings,
            purchase_pickings,
            purchase_refund_pickings,
        )

    def get_split_pickings_nogrouped(self, pickings):
        first = fields.first
        sale_pickings = pickings.filtered(
            lambda x: x.picking_type_id.code == "outgoing"
            and first(x.move_ids).location_dest_id.usage == "customer"
        )
        sale_refund_pickings = pickings.filtered(
            lambda x: x.picking_type_id.code == "incoming"
            and first(x.move_ids).location_id.usage == "customer"
        )
        purchase_pickings = pickings.filtered(
            lambda x: x.picking_type_id.code == "incoming"
            and first(x.move_ids).location_id.usage == "supplier"
        )
        purchase_refund_pickings = pickings.filtered(
            lambda x: x.picking_type_id.code == "outgoing"
            and first(x.move_ids).location_dest_id.usage == "supplier"
        )

        return (
            sale_pickings,
            sale_refund_pickings,
            purchase_pickings,
            purchase_refund_pickings,
        )

    @api.model
    def _default_journal(self, journal_type):
        """
        Get the default journal based on the given type
        :param journal_type: str
        :return: account.journal recordset
        """
        default_journal = self.env["account.journal"].search(
            [
                ("type", "=", journal_type),
                ("company_id", "=", self.env.company.id),
            ],
            limit=1,
        )
        return default_journal

    def action_generate(self):
        """
        Launch the invoice generation
        :return:
        """
        self.ensure_one()
        if self.connect_to_einvoice and self.journal_type == "purchase":
            invoices = self._action_connect_supplier_einvoice()
        else:
            invoices = self._action_generate_invoices()
        if not invoices:
            raise UserError(_("No invoice created!"))

        # Update the state on pickings related to new invoices only
        self._update_picking_invoice_status(invoices.mapped("picking_ids"))

        inv_type = self._get_invoice_type()
        if inv_type in ["out_invoice", "out_refund"]:
            xmlid = "account.action_move_out_invoice_type"
        else:
            xmlid = "account.action_move_in_invoice_type"
        action_dict = self.env["ir.actions.act_window"]._for_xml_id(xmlid)

        if len(invoices) > 1:
            action_dict["domain"] = [("id", "in", invoices.ids)]
        elif len(invoices) == 1:
            if inv_type in ["out_invoice", "out_refund"]:
                form_view = [(self.env.ref("account.view_move_form").id, "form")]
            else:
                form_view = [(self.env.ref("account.view_move_form").id, "form")]
            if "views" in action_dict:
                action_dict["views"] = form_view + [
                    (state, view)
                    for state, view in action_dict["views"]
                    if view != "form"
                ]
            else:
                action_dict["views"] = form_view
            action_dict["res_id"] = invoices.ids[0]

        return action_dict

    def _load_pickings(self):
        """
        Load pickings from context
        :return: stock.picking recordset
        """
        if self.env.context.get("active_model") == "stock.ewaybill":
            waybill_obj = self.env["stock.ewaybill"].browse(
                self.env.context.get("active_ids", [])
            )
            pickings = waybill_obj.picking_ids
            if not pickings:
                raise UserError(
                    _(
                        "No picking found! Match the waybill with a picking before creating the invoice."
                    )
                )
            pickings = pickings.filtered(lambda p: p.invoice_state == "2binvoiced")
            return pickings
        else:
            picking_obj = self.env["stock.picking"]
            active_ids = self.env.context.get("active_ids", [])
            pickings = picking_obj.browse(active_ids)
            pickings._set_as_2binvoiced()
            pickings = pickings.filtered(lambda p: p.invoice_state == "2binvoiced")
        return pickings

    def _get_journal(self):
        """
        Get the journal depending on the journal_type
        :return: account.journal recordset
        """
        self.ensure_one()
        journal_field = "%s_journal" % self.journal_type
        journal = self[journal_field]
        return journal

    def _get_invoice_type(self):
        """
        Get the invoice type
        :return: str
        """
        self.ensure_one()

        active_ids = self.env.context.get("active_ids", [])
        if active_ids:
            active_ids = active_ids[0]

        if self.env.context.get("active_model") == "stock.ewaybill":
            picking = self.env["stock.ewaybill"].browse(active_ids).picking_ids
        else:
            pick_obj = self.env["stock.picking"]
            picking = pick_obj.browse(active_ids)

        picking = self.env["stock.picking"].browse(active_ids)

        inv_type = (
            INVOICE_TYPE_MAP.get(
                (
                    picking.picking_type_code,
                    picking.location_id.usage,
                    picking.location_dest_id.usage,
                )
            )
            or "out_invoice"
        )

        return inv_type

    def _get_reference(self):
        """
        Get invoice reference
        :return: str
        """
        self.ensure_one()

        active_ids = self.env.context.get("active_ids", [])
        if active_ids:
            active_ids = active_ids[0]
        picking = self.env["stock.picking"].browse(active_ids)
        if self.journal_type == "sale":
            reference = picking.sale_id.client_order_ref
        else:
            reference = False
        return reference

    @api.model
    def _get_picking_key(self, picking):
        """
        Get the key for the given picking.
        By default, it's based on the invoice partner and the picking_type_id
        of the picking
        :param picking: stock.picking recordset
        :return: key (tuple,...)
        """
        key = (picking,)
        if self.group in ["partner", "partner_product"]:
            # Pickings with same Partner to create Invoice but the
            # Partner to Shipping is different should not be grouping.
            key = (
                picking._get_partner_to_invoice().id,
                picking.picking_type_id,
                picking.partner_id,
            )
        return key

    def _group_pickings(self, pickings):
        """
        Group given picking
        :param pickings:
        :return: list of stock.picking recordset
        """
        grouped_picking = {}
        pickings = pickings.filtered(lambda p: p.invoice_state == "2binvoiced")
        for picking in pickings:
            key = self._get_picking_key(picking)
            picks_grouped = grouped_picking.get(key, self.env["stock.picking"].browse())
            picks_grouped |= picking
            grouped_picking.update({key: picks_grouped})
        return grouped_picking.values()

    def _simulate_invoice_onchange(self, values):
        """
        Simulate onchange for invoice
        :param values: dict
        :return: dict
        """
        invoice = self.env["account.move"].new(values.copy())
        invoice._onchange_partner_id()
        invoice._inverse_company_id()
        invoice._inverse_currency_id()
        invoice._onchange_date()
        new_values = invoice._convert_to_write(invoice._cache)
        # Ensure basic values are not updated
        values.update(new_values)
        return invoice, values

    def _build_invoice_values_from_pickings(self, pickings):
        """
        Build dict to create a new invoice from given pickings
        :param pickings: stock.picking recordset
        :return: dict
        """
        picking = fields.first(pickings)
        partner = picking._get_partner_to_invoice()
        partner_shipping_id = picking.partner_id
        inv_type = self._get_invoice_type()

        if inv_type in ("out_invoice", "out_refund"):
            payment_term = (
                picking.sale_id.payment_term_id.id
                or partner.property_payment_term_id.id
            )
        else:
            payment_term = (
                picking.sale_id.payment_term_id.id
                or partner.property_supplier_payment_term_id.id
            )

        currency = self.env.company.currency_id
        if inv_type == "out_invoice":
            currency = (
                picking.sale_id.pricelist_id.currency_id
                or partner.property_product_pricelist.currency_id
                or self.env.company.currency_id
            )
        elif inv_type == "out_refund":
            currency = picking.purchase_id.currency_id or self.env.company.currency_id

        if partner:
            code = picking.picking_type_id.code
            if partner.property_product_pricelist and code == "outgoing":
                currency = partner.property_product_pricelist.currency_id
        journal = self._get_journal()
        reference = self._get_reference()
        invoice_obj = self.env["account.move"]
        values = invoice_obj.default_get(invoice_obj.fields_get().keys())
        values.update(
            {
                "user_id": picking.sales_uid.id,
                "ref": reference,
                "delivery_ref_no": picking.carrier_tracking_ref or False,
                "carrier_id": picking.carrier_id.id,
                "invoice_incoterm_id": picking.sale_id.incoterm.id,
                "comment_einvoice": picking.note,
                "pricelist_id": picking.sale_id.pricelist_id.id or False,
                "address_contact_id": partner_shipping_id,
                "invoice_payment_term_id": payment_term,
                "partner_shipping_id": partner_shipping_id,
                "fiscal_position_id": picking.sale_id.fiscal_position_id.id
                or partner.property_account_position_id.id,
                "invoice_origin": ", ".join(pickings.mapped("name")),
                "partner_id": partner.id,
                "move_type": inv_type,
                "currency_id": currency.id,
                "journal_id": journal.id,
            }
        )

        invoice, values = self._simulate_invoice_onchange(values)
        return invoice, values

    def _get_move_key(self, move):
        """
        Get the key based on the given move
        :param move: stock.move recordset
        :return: (key,)
        """
        key = (move,)
        if self.group == "partner_product":
            key = (move.product_id,)
        return key

    def _group_moves(self, moves):
        """
        Possibility to group moves (to create 1 invoice line with many moves)
        :param moves: stock.move recordset
        :return: list of stock.move recordset
        """
        grouped_moves = {}
        moves = moves.filtered(lambda m: m.invoice_state == "2binvoiced")
        for move in moves:
            key = self._get_move_key(move)
            move_grouped = grouped_moves.get(key, self.env["stock.move"].browse())
            move_grouped |= move
            grouped_moves.update({key: move_grouped})
        return grouped_moves.values()

    def _simulate_invoice_line_onchange(self, values, price_unit=None):
        """
        Simulate onchange for invoice line
        :param values: dict
        :return: dict
        """
        line = self.env["account.move.line"].new(values.copy())
        line._inverse_partner_id()
        line._inverse_product_id()
        line._inverse_account_id()
        line._inverse_amount_currency()
        new_values = line._convert_to_write(line._cache)
        if price_unit:
            new_values["price_unit"] = price_unit
        # Ensure basic values are not updated
        values.update(new_values)
        return values

    def _get_invoice_line_values(self, moves, invoice_values, invoice):
        """
        Create invoice line values from given moves
        :param moves: stock.move
        :param invoice: account.move
        :return: dict
        """
        move = fields.first(moves)
        product = move.product_id
        partner_id = self.env["res.partner"].browse(invoice_values["partner_id"])
        inv_type = invoice_values["move_type"]
        quantity = 0
        move_line_ids = []
        for move in moves:
            qty = move.product_uom_qty
            loc = move.location_id
            loc_dst = move.location_dest_id
            # Better to understand with IF/ELIF than many OR
            if inv_type == "out_invoice" and loc.usage == "customer":
                qty *= -1
            elif inv_type == "out_refund" and loc_dst.usage == "customer":
                qty *= -1
            elif inv_type == "in_invoice" and loc_dst.usage == "supplier":
                qty *= -1
            elif inv_type == "in_refund" and loc.usage == "supplier":
                qty *= -1
            quantity += qty
            move_line_ids.append((4, move.id))
        price = moves._get_price_unit_invoice()
        line_obj = self.env["account.move.line"]
        values = line_obj.default_get(line_obj.fields_get().keys())
        values.update(
            {
                "name": move.name,
                "product_id": product.id,
                "quantity": quantity,
                "price_unit": price,
                "move_line_ids": move_line_ids,
                "move_id": invoice.id,
            }
        )
        values = self._simulate_invoice_line_onchange(values, price_unit=price)
        return values

    def _update_picking_invoice_status(self, pickings):
        """
        Update invoice_state on pickings
        :param pickings: stock.picking recordset
        :return: stock.picking recordset
        """
        return pickings._set_as_invoiced()

    def ungroup_moves(self, grouped_moves_list):
        """Ungroup your moves, split them again, grouping by
        fiscal position, max itens per invoice and etc
        :param grouped_moves_list:
        :return: list of grouped moves list
        """
        return [grouped_moves_list]

    def _create_invoice(self, invoice_values):
        """Override this method if you need to change any values of the
        invoice and the lines before the invoice creation
        :param invoice_values: dict with the invoice and its lines
        :return: invoice
        """
        return self.env["account.move"].create(invoice_values)

    def _prepare_purchase_invoice_line(self, picking, move, invoice):
        """
        Prepare invoice line
        :param picking: stock.picking recordset
        :param move: stock.move recordset
        :param invoice: account.invoice recordset
        :return: values: dict
        """
        line_obj = self.env["account.move.line"]
        product = move.product_id
        categ = product.categ_id
        taxes = move._get_taxes(invoice.fiscal_position_id, invoice.type)
        partner_order_ref = move._get_partner_order_ref()
        moves_picking_ref = move._get_picking_ref()
        purchase_line = move.purchase_line_id
        if invoice.move_type in ("out_invoice", "out_refund"):
            account = product.property_account_income_id
            if not account:
                account = categ.property_account_income_categ_id
        else:
            account = product.property_account_expense_id
            if not account:
                account = categ.property_account_expense_categ_id
        account = move._get_account(invoice.fiscal_position_id, account)
        price = purchase_line.price_unit
        values = line_obj.default_get(line_obj.fields_get().keys())
        values.update(
            {
                "name": move.name,
                "account_id": account.id,
                "product_id": product.id,
                "uom_id": product.uom_id.id,
                "lot_ids": [(4, lot.id) for lot in move.move_line_ids.mapped("lot_id")],
                "quantity": move.product_uom_qty,
                "partner_order_ref": partner_order_ref,
                "moves_picking_ref": moves_picking_ref,
                "price_unit": price,
                "purchase_line_ids": [(6, 0, purchase_line.ids)],
                "invoice_line_tax_ids": [(6, 0, taxes.ids)],
                "move_line_ids": [(4, move.id)],
                "invoice_id": invoice.id,
            }
        )
        values = self._simulate_invoice_line_onchange(values, price_unit=price)
        values.update({"name": move.name})
        return values

    def _action_connect_supplier_einvoice(self):
        """
        Connect to supplier einvoice
        :return: action.invoice recordset
        """
        AccountMoveLine = self.env["account.move.line"]
        invoice = self.supplier_invoice_id
        pickings = self._load_pickings()

        if not invoice:
            raise UserError(_("You must select a supplier invoice"))

        if not invoice.invoice_line_ids:
            invoice.action_import_lines_from_einvoice_xml()

        # Link pickings with invoice
        invoice.write(
            {
                "picking_ids": [(6, 0, [p.id for p in pickings])],
            }
        )
        invoice_lines = invoice.invoice_line_ids

        for picking in pickings:
            moves = picking.mapped("move_lines")
            for move in moves:
                invoice_line = invoice_lines.filtered(
                    lambda line: line.product_id == move.product_id
                )

                if not invoice_line:
                    invoice_line = AccountMoveLine.create(
                        self._prepare_purchase_invoice_line(picking, move, invoice)
                    )
                    invoice.write({"line_ids": [(4, invoice_line.id)]})

                purchase_line = move.purchase_line_id
                # Link Invoice Lines with Move and Purchase Line
                invoice_line.write(
                    {
                        "purchase_order_id": picking.purchase_id.id,
                        "purchase_line_id": purchase_line.id,
                        # this field named wrong. it's related to stock.move,
                        # not stock.move.line
                        "move_line_ids": [(4, move.id)],
                    }
                )

                # Link Move with Invoice Lines
                move.write(
                    {
                        "invoice_line_ids": [(6, 0, invoice_line.ids)],
                    }
                )

        return invoice

    def _action_generate_invoices(self):
        """
        Action to generate invoices based on pickings
        :return: account.move recordset
        """
        pickings = self._load_pickings()
        company = pickings.mapped("company_id")
        if company and company != self.env.company:
            raise UserError(_("All pickings are not related to your company!"))
        pick_list = self._group_pickings(pickings)
        invoices = self.env["account.move"].browse()
        for pickings in pick_list:
            if True in pickings.mapped("sale_id.create_ewaybill_within_invoice"):
                pickings.filtered(
                    lambda p: p.ewaybill_id == False
                    and p.sale_id.create_ewaybill_within_invoice
                )._create_ewaybill_before_invoice(ewaybill_date=self.invoice_date)
            moves = pickings.mapped("move_ids")
            grouped_moves_list = self._group_moves(moves)
            parts = self.ungroup_moves(grouped_moves_list)
            for moves_list in parts:
                invoice, invoice_values = self._build_invoice_values_from_pickings(
                    pickings
                )
                lines = [(5, 0, {})]
                line_values = False
                for moves in moves_list:
                    line_values = self._get_invoice_line_values(
                        moves, invoice_values, invoice
                    )
                    if line_values:
                        lines.append((0, 0, line_values))
                if line_values:  # Only create the invoice if it has lines
                    invoice_values["invoice_line_ids"] = lines
                    # Necessary inform line_ids with empty list to avoid error
                    # addons/account/models/account_move.py", line 2251,
                    # in _sanitize_vals
                    # assert command not in (Command.SET, Command.CLEAR)
                    invoice_values["line_ids"] = []
                    invoice_values["invoice_date"] = self.invoice_date
                    invoice = self._create_invoice(invoice_values)
                    invoice._compute_amount()
                    for move in moves_list:
                        if move.sale_line_id:
                            move.sale_line_id.invoice_lines = move.invoice_line_ids
                    invoices |= invoice
        return invoices
