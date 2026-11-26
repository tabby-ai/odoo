import { patch } from "@web/core/utils/patch";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { PosPayment } from "@point_of_sale/app/models/pos_payment";
import { PosStore } from "@point_of_sale/app/services/pos_store";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { _t } from "@web/core/l10n/translation";
import { PaymentTabby, TABBY_IN_FLIGHT } from "@pos_tabby/app/payment_tabby";

// After a reload the order comes back with its Tabby line still "waiting":
// follow it again, so a payment the customer completes is not lost and a
// cancel reaches Tabby.
patch(PaymentScreen.prototype, {
    onMounted() {
        const result = super.onMounted(...arguments);
        const order = this.pos.getOrder();
        for (const line of order?.payment_ids || []) {
            const terminal = line.payment_method_id?.payment_terminal;
            if (terminal instanceof PaymentTabby && TABBY_IN_FLIGHT.includes(line.getPaymentStatus())) {
                terminal.resumeLine(order, line);
            }
        }
        return result;
    },
});

// No "Force done" or "Force cancel" on a Tabby line: only Tabby decides it
// (the server also refuses a paid order whose Tabby line Tabby did not confirm).
patch(PosPayment.prototype, {
    isForceDoneAllowed() {
        if (this.payment_method_id?.use_payment_terminal === "tabby") {
            return false;
        }
        return super.isForceDoneAllowed(...arguments);
    },
});

// Deleting an order drops its lines without asking the terminal: an order
// whose Tabby line is paid or still in flight would leave the customer
// charged for a sale that no longer exists.
patch(PosStore.prototype, {
    async _onBeforeDeleteOrder(order) {
        const held = (order?.payment_ids || []).some(
            (line) =>
                line.payment_method_id?.payment_terminal instanceof PaymentTabby &&
                ["done", ...TABBY_IN_FLIGHT].includes(line.getPaymentStatus())
        );
        if (held) {
            this.dialog.add(AlertDialog, {
                title: _t("Tabby"),
                body: _t(
                    "This order has a Tabby payment. Finish the order, or remove the Tabby line first (a paid Tabby payment is refunded from the Tabby merchant dashboard)."
                ),
            });
            return false;
        }
        return super._onBeforeDeleteOrder(...arguments);
    },
});
