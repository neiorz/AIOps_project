output "aiops_namespace" {
  description = "Namespace where AIOps platform runs"
  value       = kubernetes_namespace.aiops.metadata[0].name
}

output "chaos_mesh_namespace" {
  description = "Namespace where Chaos Mesh operates"
  value       = kubernetes_namespace.chaos_mesh.metadata[0].name
}

output "monitoring_namespace" {
  description = "Namespace where the in-cluster observability backends live"
  value       = kubernetes_namespace.monitoring.metadata[0].name
}

output "chaos_mesh_release" {
  description = "Chaos Mesh helm release, pinned so plan and apply agree"
  value       = "${helm_release.chaos_mesh.name}/${helm_release.chaos_mesh.version}"
}

