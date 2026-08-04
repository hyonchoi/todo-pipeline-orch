# Plan is the execution authority

`Plan:` is the execution authority and the sole field that makes a TODO pipeline-actionable. `Spec:` is the authoritative outcome contract, and `Reference:` supplies non-authoritative context; when both a Plan and Spec exist, workers execute the Plan and validate the result against the Spec. This replaces the prior implication that `Spec:` drives execution and keeps execution instructions distinct from acceptance requirements.
