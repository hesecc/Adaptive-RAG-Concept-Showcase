"""Values shared by tests that sign in as the fixed administrator.

The password only exists in tests. Real deployments set RAG_ADMIN_PASSWORD in their own
environment, so no password is hard-coded in the application.
"""

ADMIN_PASSWORD = "test-admin-password"
