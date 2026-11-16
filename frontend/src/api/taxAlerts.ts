// tax-alerts — unrealized loss harvesting opportunities.
import { api } from "./client";

export interface TaxLossAlert {
  security_id: number;
  symbol: string;
  account_id: number;
  account_name: string;
  quantity: string;
  book_cost_cad: string;
  market_value_cad: string;
  unrealized_loss_cad: string;
  estimated_tax_benefit_cad: string;
  superficial_loss_risk: boolean;
}

export interface TaxAlertsResponse {
  alerts: TaxLossAlert[];
  summary: {
    count: number;
    total_unrealized_loss_cad: string;
    total_estimated_tax_benefit_cad: string;
  };
}

export function getTaxLossAlerts(): Promise<TaxAlertsResponse> {
  return api<TaxAlertsResponse>("/taxes/alerts");
}
