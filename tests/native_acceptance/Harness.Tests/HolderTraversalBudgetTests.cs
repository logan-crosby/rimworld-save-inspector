using Rwsi.NativeAcceptance;
using Xunit;

public class HolderTraversalBudgetTests
{
    [Fact]
    public void DetectsReferenceCycleOnCurrentAncestorPath()
    {
        var root = new object();
        var child = new object();
        var budget = new HolderTraversalBudget<object>(maxDepth: 4, maxNodes: 10);

        Assert.Null(budget.TryEnter(root, 0));
        Assert.Null(budget.TryEnter(child, 1));
        Assert.Equal("cycle", budget.TryEnter(root, 2));

        budget.Exit(child);
        budget.Exit(root);
    }

    [Fact]
    public void RejectsDepthBudgetExhaustion()
    {
        var depthBudget = new HolderTraversalBudget<object>(maxDepth: 1, maxNodes: 10);
        Assert.Null(depthBudget.TryEnter(new object(), 0));
        Assert.Null(depthBudget.TryEnter(new object(), 1));
        Assert.Equal("depth_limit", depthBudget.TryEnter(new object(), 2));
    }

    [Fact]
    public void RejectsTotalNodeBudgetExhaustion()
    {
        var nodeBudget = new HolderTraversalBudget<object>(maxDepth: 4, maxNodes: 2);
        Assert.Null(nodeBudget.TryEnter(new object(), 0));
        Assert.Null(nodeBudget.TryEnter(new object(), 1));
        Assert.Equal("node_limit", nodeBudget.TryEnter(new object(), 2));
        Assert.Equal(2, nodeBudget.Visited);
    }
}
