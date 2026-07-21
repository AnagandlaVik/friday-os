class ToolPolicy:
    """
    Defines a policy for which tools are allowed to be executed.
    """

    def __init__(self, allowed_operations: set[str]):
        """
        Initializes the policy with a set of allowed operation names.
        """
        self._allowed_operations = allowed_operations

    def is_allowed(self, operation: str) -> bool:
        """
        Checks if an operation is allowed by the policy.
        """
        return operation in self._allowed_operations
