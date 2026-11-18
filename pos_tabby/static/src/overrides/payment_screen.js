import { patch } from "@web/core/utils/patch";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { PosPayment } from "@point_of_sale/app/models/pos_payment";
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
