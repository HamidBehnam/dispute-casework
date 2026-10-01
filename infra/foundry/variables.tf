variable "budget_emails" {
  type        = list(string)
  description = "Recipients of the subscription budget alerts. Supplied through TF_VAR_budget_emails."
}
