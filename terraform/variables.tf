variable "kubeconfig_path" {
  description = "Path to the kubeconfig file"
  type        = "string"
  default     = "~/.kube/config"
}

variable "namespace" {
  description = "Kubernetes namespace for AIOps core engine"
  type        = "string"
  default     = "aiops"
}

variable "boutique_namespace" {
  description = "Namespace for Google Cloud Online Boutique microservices"
  type        = "string"
  default     = "default"
}
