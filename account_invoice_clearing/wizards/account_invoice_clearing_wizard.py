# Copyright 2023 Moduon Team S.L.
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl-3.0)

import json
from collections import OrderedDict
from itertools import groupby

from odoo import _, api, exceptions, fields, models
from odoo.tools import float_is_zero


class AccountInvoiceClearingWizard(models.TransientModel):
    _name = "account.invoice.clearing.wizard"
    _description = "Invoice Clearing Wizard"
    _transient_max_hours = 6  # 6 hours to allow vacuuming

    @api.model
    def _get_default_journal(self):
        return self.env["account.journal"].search(
            [
                ("company_id", "=", self.env.company.id),
                ("type", "=", "general"),
            ],
            limit=1,
        )

    move_name = fields.Char(
        help="Name of the move generated by this wizard",
        default="Clearing operation",
    )
    move_line_prefix = fields.Char(
        string="Prefix for move lines",
        help="Prefix to be used in the name of the move lines generated by this wizard",
        default="Clearing operation",
    )
    invoice_ids = fields.Many2many(
        comodel_name="account.move",
        domain=[("move_type", "!=", "entry"), ("state", "=", "posted")],
        string="Invoices/Bills",
        required=True,
    )
    move_line_ids = fields.Many2many(
        comodel_name="account.move.line",
        compute="_compute_initial_data",
        store=True,
    )
    company_id = fields.Many2one(
        comodel_name="res.company",
        compute="_compute_initial_data",
        store=True,
    )
    company_currency_id = fields.Many2one(
        comodel_name="res.currency",
        compute="_compute_initial_data",
        store=True,
    )
    move_type = fields.Char(
        compute="_compute_initial_data",
        store=True,
    )
    commercial_partner_id = fields.Many2one(
        comodel_name="res.partner",
        compute="_compute_initial_data",
        store=True,
    )
    amount_to_clear = fields.Monetary(
        compute="_compute_amount_to_clear",
        currency_field="company_currency_id",
        store=False,
    )
    line_ids = fields.One2many(
        comodel_name="account.invoice.clearing.lines.wizard",
        inverse_name="clearing_id",
        string="Lines",
    )
    date = fields.Date(
        default=fields.Date.context_today,
        required=True,
    )
    journal_id = fields.Many2one(
        comodel_name="account.journal",
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]",
        string="Journal",
        check_company=True,
        required=True,
        default=_get_default_journal,
    )
    # Preview resulting move
    move_data = fields.Text(
        compute="_compute_move_data",
        help="JSON value of the moves to be created",
    )
    preview_move_data = fields.Text(
        compute="_compute_preview_move_data",
        help="JSON value of the data to be displayed in the previewer",
    )

    def default_get(self, fields):
        res = super().default_get(fields)
        active_model = self.env.context.get("active_model")
        active_ids = self.env.context.get("active_ids")
        if active_model == "account.move" and active_ids:
            res["invoice_ids"] = [(6, 0, active_ids)]
        return res

    @api.model
    def _get_internal_type_from_move_type(self, move_type, is_counterpart=False):
        move_types = ["payable", "receivable"]
        if is_counterpart and "refund" not in move_type:
            move_types.reverse()
        if move_type.startswith("out_"):
            move_types.reverse()
        return move_types[0]

    # Computes & Onchange
    @api.depends("invoice_ids")
    def _compute_initial_data(self):
        """Compute initial data for the wizard."""
        self.update(
            {
                "move_type": False,
                "commercial_partner_id": False,
                "company_id": False,
                "company_currency_id": False,
                "move_line_ids": False,
            }
        )
        for record in self:
            if not record.invoice_ids:
                continue
            company = self._get_companies(record.invoice_ids)[0]
            record.move_type = self._get_move_types(record.invoice_ids)[0]
            record.commercial_partner_id = self._get_commercial_partners(
                record.invoice_ids
            )[0]
            record.company_id = company
            record.company_currency_id = company.currency_id
            internal_type = self._get_internal_type_from_move_type(record.move_type)
            record.move_line_ids = self.invoice_ids.mapped("line_ids").filtered(
                lambda ml: not ml.full_reconcile_id
                and ml.balance
                and ml.account_id.reconcile
                and ml.account_id.internal_type == internal_type
            )

    @api.depends("move_line_ids", "line_ids")
    def _compute_move_data(self):
        """Compute the data to be used by the account.move form view."""
        self.update({"move_data": "[]"})
        for record in self:
            if not record.move_line_ids or not record.line_ids:
                continue
            record.move_data = json.dumps(
                record.with_context(preview=True)._get_move_dict_vals()
            )

    @api.depends("move_data")
    def _compute_preview_move_data(self):
        """Compute the data to be displayed in the previewer."""
        am_model = self.env["account.move"]
        for record in self:
            preview_columns = [
                {"field": "account_id", "label": _("Account")},
                {"field": "name", "label": _("Label")},
                {"field": "partner_id", "label": _("Partner")},
                {
                    "field": "debit",
                    "label": _("Debit"),
                    "class": "text-right text-nowrap",
                },
                {
                    "field": "credit",
                    "label": _("Credit"),
                    "class": "text-right text-nowrap",
                },
            ]
            move_vals = json.loads(record.move_data)
            preview_data = []
            for move in move_vals:
                preview_vals = am_model._move_dict_to_preview_vals(
                    move,
                    record.company_currency_id,
                )
                preview_vals["group_name"] = _(
                    "Preview of the clearing move related to %s",
                    move["related_to_move"],
                )
                preview_data += [preview_vals]

            record.preview_move_data = json.dumps(
                {
                    "groups_vals": preview_data,
                    "options": {
                        "discarded_number": False,
                        "columns": preview_columns,
                    },
                }
            )

    def _compute_amount_to_clear(self):
        """Compute the remaining amount to clear."""
        for record in self:
            residual_amount = sum(record.move_line_ids.mapped("amount_residual"))
            amount_cleared = sum(record.line_ids.mapped("amount_to_clear"))
            record.amount_to_clear = residual_amount + amount_cleared

    @api.onchange("move_type")
    def _onchange_move_type(self):
        """Create lines for all invoices of the counterpart types."""
        data = [(5, 0, 0)]
        if self.move_type and self.commercial_partner_id:
            for idx, move_line in enumerate(
                self._get_available_clearing_move_lines(
                    self.move_type, self.commercial_partner_id, self.move_line_ids
                ),
                10,
            ):
                data.append((0, 0, {"move_line_id": move_line.id, "sequence": idx}))
        self.line_ids = data

    # Actions
    def action_reopen_wizard(self):
        """Reopen the wizard."""
        self.ensure_one()
        action = self.env.ref(
            "account_invoice_clearing.action_account_invoice_clearing_wizard"
        ).read()[0]
        action["res_id"] = self.id
        return action

    def action_reset_lines(self):
        """Reset all lines."""
        self.ensure_one()
        self.action_unlink_lines()
        self.action_add_lines()
        return self.action_reopen_wizard()

    def action_fill_amount_to_clear(self):
        """Fill the amount to clear in all lines until amount to clear is zero."""
        self.ensure_one()
        # Always inverse sign because move lines has negative residual amounts
        amount_to_clear = -self.amount_to_clear
        for line in self.line_ids.filtered(lambda l: not l.amount_to_clear):
            cmp_fnc = float.__le__ if line.amount_residual < 0.0 else float.__ge__
            if float_is_zero(
                amount_to_clear, precision_rounding=line.company_currency_id.rounding
            ):
                line.amount_to_clear = 0.0
            elif cmp_fnc(amount_to_clear, line.amount_residual):
                line.amount_to_clear = line.amount_residual
                amount_to_clear -= line.amount_residual
            else:
                line.amount_to_clear = amount_to_clear
                amount_to_clear = 0.0
        return self.action_reopen_wizard()

    def action_unlink_lines(self):
        """Unlink all lines."""
        self.ensure_one()
        self.line_ids = [(5, 0, 0)]
        return self.action_reopen_wizard()

    def action_add_lines(self):
        """Add all possible lines."""
        self.ensure_one()
        existing_move_lines = self.line_ids.mapped("move_line_id")
        last_sequence = self.line_ids[-1].sequence if self.line_ids else 0
        new_lines = []
        for move_line in self._get_available_clearing_move_lines(
            self.move_type, self.commercial_partner_id, self.move_line_ids
        ):
            if move_line in existing_move_lines:
                continue
            new_lines.append(
                (0, 0, {"move_line_id": move_line.id, "sequence": last_sequence + 1})
            )
            last_sequence += 10
        if new_lines:
            self.line_ids = new_lines
        return self.action_reopen_wizard()

    def action_sort_by_date_due(self, reverse=False):
        """Sort lines by date due."""
        self.ensure_one()
        sorted_lines = self.line_ids.sorted(
            key=lambda l: l.date_maturity, reverse=reverse
        )
        for seq, line in enumerate(sorted_lines, start=10):
            line.sequence = seq
        return self.action_reopen_wizard()

    def action_sort_by_date_due_asc(self):
        """Sort lines by date due ascending."""
        return self.action_sort_by_date_due(reverse=False)

    def action_sort_by_date_due_desc(self):
        """Sort lines by date due descending."""
        return self.action_sort_by_date_due(reverse=True)

    def action_sort_by_residual(self, reverse=False):
        """Sort lines by residual amount."""
        self.ensure_one()
        sorted_lines = self.line_ids.sorted(
            key=lambda l: l.amount_residual, reverse=reverse
        )
        for seq, line in enumerate(sorted_lines, start=10):
            line.sequence = seq
        return self.action_reopen_wizard()

    def action_sort_by_residual_asc(self):
        """Sort lines by residual amount ascending."""
        return self.action_sort_by_residual(reverse=False)

    def action_sort_by_residual_desc(self):
        """Sort lines by residual amount descending."""
        return self.action_sort_by_residual(reverse=True)

    def button_confirm(self):
        """Create the clearing move."""
        self.ensure_one()
        am_model = self.env["account.move"]
        aml_model = self.env["account.move.line"]
        # Get the fresh resulting moves data
        res_ids = []
        moves_data = self._get_move_dict_vals()
        for move_data in moves_data:
            # Extract in order the move_line wich every line needs to be reconciled
            to_concile_lines = []
            for move_line in move_data["line_ids"]:
                to_concile_lines.append(
                    aml_model.browse(move_line[2]["move_line_id_to_reconcile"])
                )
                del move_line[2]["move_line_id_to_reconcile"]
            # Create move with cleaned data
            del move_data["related_to_move"]
            move = am_model.create(move_data)
            move.action_post()
            res_ids.append(move.id)
            for idx, move_line in enumerate(move.line_ids):
                (move_line | to_concile_lines[idx]).reconcile()
        # Build resulting action
        action = {
            "name": _("Resulting Clearing Move"),
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "view_mode": "form",
            "target": "current",
        }
        if len(res_ids) > 1:
            action.update(
                {
                    "domain": [("id", "in", res_ids)],
                    "view_mode": "tree,form",
                }
            )
        else:
            action["res_id"] = res_ids[0]
        return action

    # Helpers
    @api.model
    def _get_commercial_partners(self, invoices):
        """Get the commercial partners of the invoices."""
        partners = invoices.mapped("commercial_partner_id")
        return partners.mapped("commercial_partner_id")

    @api.model
    def _get_companies(self, invoices):
        """Get the companies of the invoices."""
        return invoices.mapped("company_id")

    @api.model
    def _get_move_types(self, invoices):
        """Get the move types of the invoices."""
        return list(set(invoices.mapped("move_type")))

    @api.model
    def _get_available_clearing_move_lines(self, move_type, partner, move_lines):
        """Get the move lines that can be used for clearing."""
        counterpart_internal_type = self._get_internal_type_from_move_type(
            move_type, is_counterpart=True
        )
        debit_credit_field = (
            "credit" if sum(move_lines.mapped("amount_residual")) > 0 else "debit"
        )
        return (
            self.env["account.move.line"]
            .sudo()
            .search(
                [
                    ("id", "not in", move_lines.ids),
                    ("account_id.internal_type", "=", counterpart_internal_type),
                    ("reconciled", "=", False),
                    ("partner_id", "child_of", partner.id),
                    ("parent_state", "=", "posted"),
                    ("account_id.reconcile", "=", True),
                    (debit_credit_field, ">", 0.0),
                ],
                order="date_maturity asc, amount_residual asc",
            )
        )

    def _get_remaining_amount_to_clear(self):
        """Get the remaining amount to clear."""
        self.ensure_one()
        return self.amount_to_clear + sum(self.line_ids.mapped("amount_to_clear"))

    # flake8: noqa: C901
    def _get_move_dict_vals(self):
        self.ensure_one()
        move_name, move_line_prefix = self.move_name, self.move_line_prefix
        if self.env.context.get("preview"):
            move_name, move_line_prefix = "<Move Name>", "<Move Line Prefix>"

        def fiz(amount):
            return float_is_zero(
                amount, precision_rounding=self.company_currency_id.rounding
            )

        def get_debit_credit(amount):
            if amount > 0.0:
                return amount, 0.0
            return 0.0, 0.0 if fiz(amount) else -amount

        def get_amount_to_use(amount_to_fill, avaiable_amount):
            amount = max(amount_to_fill, avaiable_amount)
            if amount_to_fill > 0.0:
                amount = min(amount_to_fill, avaiable_amount)
            return self.company_currency_id.round(amount)

        def build_line_name(move_line):
            txts = [move_line_prefix, move_line.move_id.name, move_line.name]
            if move_line.move_id.name == move_line.name:
                txts[1:] = [move_line.move_id.name]
            return " - ".join(list(map(str.strip, filter(lambda t: t, txts))))

        def get_clear_sign_modifier(a, b):
            different_sign = (a < 0.0 and b > 0.0) or (a > 0.0 and b < 0.0)
            return -1.0 if different_sign else 1.0

        clear_lines_availability = OrderedDict()
        for cl in self.line_ids.filtered(lambda l: l.amount_to_clear):
            clear_lines_availability[cl.move_line_id] = cl.amount_to_clear

        move_vals = []
        for move, move_lines in groupby(self.move_line_ids, key=lambda ml: ml.move_id):
            line_vals = []
            for move_line in move_lines:
                related_move_line_vals = []

                ml_amount_residual, cl_balance = move_line.amount_residual, 0.0
                for cl_move_line in clear_lines_availability:
                    # Move line fully cleared
                    if fiz(ml_amount_residual):
                        break
                    # Clearing line fully used
                    if fiz(clear_lines_availability[cl_move_line]):
                        continue

                    # Choose correct sign to compute clearing amount
                    clear_sign = get_clear_sign_modifier(
                        ml_amount_residual, clear_lines_availability[cl_move_line]
                    )

                    # Used amount for clearing (sign aligned)
                    used_amount = get_amount_to_use(
                        ml_amount_residual,
                        clear_sign * clear_lines_availability[cl_move_line],
                    )
                    # Clearing balance for this move line
                    cl_balance += clear_sign * used_amount
                    # Remaining amount to clear on this move
                    ml_amount_residual -= used_amount
                    # Remaining amount on this clearing move line
                    clear_lines_availability[cl_move_line] += used_amount
                    cl_debit, cl_credit = get_debit_credit(used_amount)
                    related_move_line_vals.append(
                        {
                            "name": build_line_name(cl_move_line),
                            "debit": cl_debit,
                            "credit": cl_credit,
                            "account_id": cl_move_line.account_id.id,
                            "partner_id": self.commercial_partner_id.id,
                            "currency_id": self.company_currency_id.id,
                            "move_line_id_to_reconcile": cl_move_line.id,
                        }
                    )
                ml_debit, ml_credit = get_debit_credit(cl_balance)
                if fiz(ml_debit) and fiz(ml_credit):
                    continue
                line_vals.append(
                    {
                        "name": build_line_name(move_line),
                        "debit": ml_debit,
                        "credit": ml_credit,
                        "account_id": move_line.account_id.id,
                        "partner_id": self.commercial_partner_id.id,
                        "currency_id": self.company_currency_id.id,
                        "move_line_id_to_reconcile": move_line.id,
                    }
                )
                line_vals.extend(related_move_line_vals)

            if not line_vals:
                continue
            move_vals.append(
                {
                    "currency_id": self.company_currency_id.id,
                    "move_type": "entry",
                    "journal_id": self.journal_id.id,
                    "date": fields.Date.to_string(self.date),
                    "ref": move_name,
                    "line_ids": [(0, 0, line) for line in line_vals],
                    "related_to_move": move.name,
                }
            )
        return move_vals

    # Constrains
    @api.constrains("invoice_ids")
    def _check_invoices_same_partner(self):
        """Check that all invoices are from the same
        commercial partner, type and company."""
        for record in self:
            if len(set(self._get_commercial_partners(record.invoice_ids))) > 1:
                raise exceptions.ValidationError(
                    _("Invoices must be from the same commercial partner.")
                )
            if len(self._get_move_types(record.invoice_ids)) > 1:
                raise exceptions.ValidationError(
                    _("Invoices must be of the same type.")
                )
            if len(self._get_companies(record.invoice_ids)) > 1:
                raise exceptions.ValidationError(
                    _("Invoices must belong to the same company.")
                )


class AccountInvoiceClearingLinesWizard(models.TransientModel):
    _name = "account.invoice.clearing.lines.wizard"
    _description = "Account Invoice Clearing Lines Wizard"
    _order = "sequence, id"

    clearing_id = fields.Many2one(
        comodel_name="account.invoice.clearing.wizard",
        string="Clearing",
    )
    sequence = fields.Integer(
        default=10,
    )
    move_line_id = fields.Many2one(
        comodel_name="account.move.line",
        string="Move Line",
        required=True,
    )
    invoice_id = fields.Many2one(
        related="move_line_id.move_id",
        readonly=True,
    )
    date = fields.Date(
        related="move_line_id.date",
        readonly=True,
    )
    date_maturity = fields.Date(
        related="move_line_id.date_maturity",
        readonly=True,
    )
    company_currency_id = fields.Many2one(
        related="move_line_id.company_currency_id",
        readonly=True,
    )
    amount_residual = fields.Monetary(
        related="move_line_id.amount_residual",
        currency_field="company_currency_id",
        readonly=True,
    )
    debit = fields.Monetary(
        related="move_line_id.debit",
        currency_field="company_currency_id",
        readonly=True,
    )
    credit = fields.Monetary(
        related="move_line_id.credit",
        currency_field="company_currency_id",
        readonly=True,
    )
    account_id = fields.Many2one(
        related="move_line_id.account_id",
        readonly=True,
    )
    name = fields.Char(
        related="move_line_id.name",
        readonly=True,
    )
    product_id = fields.Many2one(
        related="move_line_id.product_id",
        readonly=True,
    )
    analytic_account_id = fields.Many2one(
        related="move_line_id.analytic_account_id",
        readonly=True,
    )
    analytic_tag_ids = fields.Many2many(
        related="move_line_id.analytic_tag_ids",
        readonly=True,
    )
    amount_to_clear = fields.Monetary(
        string="Clearing amount",
        currency_field="company_currency_id",
        default=0.0,
        readonly=True,
    )
    can_use_line = fields.Boolean(
        compute="_compute_can_use_line",
        store=False,
    )

    # Compute
    @api.depends("amount_residual")
    def _compute_can_use_line(self):
        """Compute if the line can be used for clearing."""
        self.update({"can_use_line": False})
        for record in self:
            if record.clearing_id.amount_to_clear == 0.0:
                continue
            record.can_use_line = record.amount_residual != record.amount_to_clear

    # Actions
    def action_use_all_amount_residual(self):
        """Fill the amount to clear with the residual amount."""
        self.ensure_one()
        clearing = self.clearing_id
        if float_is_zero(
            clearing.amount_to_clear,
            precision_digits=clearing.company_currency_id.rounding,
        ):
            return clearing.action_reopen_wizard()
        # Always inverse sign because move lines has negative residual amounts
        amount_to_clear = -clearing.amount_to_clear
        cmp_fnc = float.__le__ if self.amount_residual < 0.0 else float.__ge__
        if cmp_fnc(amount_to_clear, self.amount_residual):
            amount_to_clear = self.amount_residual
        self.amount_to_clear = amount_to_clear
        return clearing.action_reopen_wizard()

    # Constrains
    @api.constrains("amount_to_clear")
    def _check_amount_to_clear(self):
        """Check that the amount to clear is not greater than the residual amount."""
        for record in self:
            if abs(record.amount_to_clear) > abs(record.amount_residual):
                raise exceptions.ValidationError(
                    _("Amount to clear cannot be greater than residual amount.")
                )
