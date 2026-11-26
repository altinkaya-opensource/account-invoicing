# Copyright 2026 Ahmet Yiğit Budak (https://github.com/yibudak)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).
"""Open the Incoming Shipments list empty.

No receipt was linked to its vendor bill for a year, so the list would open
with every receipt of that time. Nobody is to link them: they are set as not
billable, as the button of a receipt does. A move a bill line is linked to
keeps its state.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    receipts = env["stock.picking"].search([("unbilled_receipt", "=", True)])
    if not receipts:
        return
    moves = receipts.move_ids._unbilled_supplier_receipts()
    # SQL: a write would recompute the status of the sale orders some of
    # these receipts supply, and that status does not read this state.
    cr.execute(
        "UPDATE stock_picking SET invoice_state = 'none' WHERE id IN %s",
        [tuple(receipts.ids)],
    )
    cr.execute(
        "UPDATE stock_move SET invoice_state = 'none' WHERE id IN %s",
        [tuple(moves.ids)],
    )
    env.invalidate_all()
    _logger.info("%s unbilled receipts set as not billable", len(receipts))
