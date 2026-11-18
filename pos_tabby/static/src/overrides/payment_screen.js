import { patch } from "@web/core/utils/patch";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
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
