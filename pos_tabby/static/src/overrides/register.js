import { register_payment_method } from "@point_of_sale/app/store/pos_store";
import { PaymentTabby } from "@pos_tabby/app/payment_tabby";

register_payment_method("tabby", PaymentTabby);
