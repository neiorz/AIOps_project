variable "kubeconfig_path" {
  description = "Path to the kubeconfig file"
  type        = string
  default     = "~/.kube/config"
}

variable "namespace" {
  description = "Kubernetes namespace for AIOps core engine"
  type        = string
  default     = "aiops"
}

variable "boutique_namespace" {
  description = "Namespace for Google Cloud Online Boutique microservices"
  type        = string
  default     = "default"
}

# --- Track T7 : Chaos Mesh --------------------------------------------------
# These describe the *cluster*, not the platform, so they are overridable
# rather than hard-coded: minikube here runs docker, a managed cluster would
# run containerd.

variable "chaos_mesh_repository" {
  description = "Chaos Mesh chart repository. Note the old chaos-mesh.github.io/chaos-mesh URL now 404s."
  type        = string
  default     = "https://charts.chaos-mesh.org"
}

variable "chaos_mesh_version" {
  description = "Pinned Chaos Mesh chart version, so plan/apply stay reproducible."
  type        = string
  default     = "2.8.4"
}

variable "chaos_daemon_runtime" {
  description = "Container runtime the Chaos Mesh daemon drives (docker or containerd)."
  type        = string
  default     = "docker"
}

variable "chaos_daemon_socket_path" {
  description = "Runtime socket. /var/run/docker.sock for docker; /run/containerd/containerd.sock for containerd."
  type        = string
  default     = "/var/run/docker.sock"
}

