# Copyright 2026 Ahmet Yiğit Budak (https://github.com/yibudak)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).
"""Give vendor bills back the receipts the connect wizard dropped.

The wizard wrote a bill's pickings itself, replacing those an earlier run had
linked, although the field is computed from the stock moves of the bill's
lines. The lines kept their moves, so the pickings are read from them again.
SQL, because a posted bill is not to be written through the ORM for this.
"""


def migrate(cr, version):
    cr.execute(
        """
        INSERT INTO account_move_stock_picking_rel (account_move_id, stock_picking_id)
        SELECT DISTINCT line.move_id, move.picking_id
          FROM stock_move_invoice_line_rel rel
          JOIN account_move_line line ON line.id = rel.invoice_line_id
          JOIN account_move bill ON bill.id = line.move_id
          JOIN stock_move move ON move.id = rel.move_id
         WHERE bill.move_type = 'in_invoice'
           AND move.picking_id IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM account_move_stock_picking_rel known
                WHERE known.account_move_id = line.move_id
                  AND known.stock_picking_id = move.picking_id
           )
        """
    )
