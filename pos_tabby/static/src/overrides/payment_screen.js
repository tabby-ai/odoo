import { patch } from "@web/core/utils/patch";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { PosStore } from "@point_of_sale/app/store/pos_store";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { _t } from "@web/core/l10n/translation";
import { PaymentTabby, TABBY_IN_FLIGHT } from "@pos_tabby/app/payment_tabby";

// After a reload the order comes back with its Tabby line still "waiting":
// follow it again, so a payment the customer completes is not lost and a
// cancel reaches Tabby.
patch(PaymentScreen.prototype, {
    onMounted() {
        super.onMounted(...arguments);
        const order = this.pos.get_order();
        for (const line of order?.payment_ids || []) {
            const terminal = line.payment_method_id?.payment_terminal;
            if (terminal instanceof PaymentTabby && TABBY_IN_FLIGHT.includes(line.get_payment_status())) {
                terminal.resumeLine(order, line);
            }
        }
    },
});

// Deleting an order drops its lines without asking the terminal: an order
// whose Tabby line is paid or still in flight would leave the customer
// charged for a sale that no longer exists.
patch(PosStore.prototype, {
    async _onBeforeDeleteOrder(order) {
        let held = false;
        for (const line of order?.payment_ids || []) {
            const terminal = line.payment_method_id?.payment_terminal;
            if (!(terminal instanceof PaymentTabby) || !["done", ...TABBY_IN_FLIGHT].includes(line.get_payment_status())) {
                continue;
            }
            if (line.amount < 0) {
                held = true; // a refund that went out stays with its return
                break;
            }
            // The line may look paid while Tabby no longer holds the money
            // (refunded in the dashboard): ask the server, which reads Tabby.
            const res = await terminal._rpc("tabby_payment_status", terminal._lineData(order, line));
            if (res?.outcome !== "failed") {
                held = true;
                break;
            }
        }
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
