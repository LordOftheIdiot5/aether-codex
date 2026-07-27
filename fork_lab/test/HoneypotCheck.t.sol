// SPDX-License-Identifier: MIT
pragma solidity ^0.8.13;

import "forge-std/Test.sol";

interface IUniV2Router {
    function swapExactETHForTokensSupportingFeeOnTransferTokens(
        uint256 amountOutMin, address[] calldata path, address to, uint256 deadline
    ) external payable;
    function swapExactTokensForETHSupportingFeeOnTransferTokens(
        uint256 amountIn, uint256 amountOutMin, address[] calldata path, address to, uint256 deadline
    ) external;
    function addLiquidityETH(
        address token, uint256 amountTokenDesired, uint256 amountTokenMin,
        uint256 amountETHMin, address to, uint256 deadline
    ) external payable returns (uint256, uint256, uint256);
}

interface IUniV2Factory {
    function createPair(address, address) external returns (address);
}

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
}

/// @title Fork-based honeypot & tax checker — the CORE DIFFERENTIATOR of the tool.
/// @notice Static rug-checkers (TokenSniffer/Rugcheck) can be gamed — scammers write
///         contracts that pass them. This can't be gamed: we fork real mainnet, actually
///         BUY the token, then actually try to SELL it. If the sell reverts, it's a
///         honeypot — proven, not guessed. The round-trip loss reveals hidden buy/sell tax.
contract HoneypotCheck is Test {
    IUniV2Router constant ROUTER = IUniV2Router(0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D);
    address constant WETH = 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2;
    address constant PEPE = 0x6982508145454Ce325dDbE47a25d4ec3d2311933; // known-good, sellable

    function setUp() public {
        vm.createSelectFork("mainnet", 20000000);
    }

    /// Ethereum-default wrapper (used by the PEPE / honeypot unit tests).
    function _check(address token) internal returns (bool, uint256) {
        return _checkOn(token, address(ROUTER), WETH);
    }

    /// @notice Chain-agnostic buy-then-sell test. Works on any Uniswap-V2-style DEX
    ///         (Uniswap on ETH, PancakeSwap on BSC, ...) — pass its router + wrapped native.
    /// @return sellable false = honeypot (bought but can't sell)
    /// @return lossBps round-trip loss in basis points (AMM fee + slippage + any token tax)
    function _checkOn(address token, address routerAddr, address wnative)
        internal
        returns (bool sellable, uint256 lossBps)
    {
        IUniV2Router router = IUniV2Router(routerAddr);
        uint256 buyNative = 0.1 ether; // 0.1 of the chain's native coin (ETH/BNB)
        vm.deal(address(this), 1 ether);

        // BUY: native -> token (fee-on-transfer-safe path handles tax tokens)
        address[] memory buyPath = new address[](2);
        buyPath[0] = wnative;
        buyPath[1] = token;
        try router.swapExactETHForTokensSupportingFeeOnTransferTokens{value: buyNative}(
            0, buyPath, address(this), block.timestamp
        ) {} catch {
            return (false, 10000); // can't even buy — dead/unbuyable
        }

        uint256 tokBal = IERC20(token).balanceOf(address(this));
        if (tokBal == 0) return (false, 10000);

        // SELL: token -> native. This is the honeypot test — scam tokens revert HERE.
        IERC20(token).approve(routerAddr, tokBal);
        address[] memory sellPath = new address[](2);
        sellPath[0] = token;
        sellPath[1] = wnative;
        uint256 nativeBefore = address(this).balance;
        try router.swapExactTokensForETHSupportingFeeOnTransferTokens(
            tokBal, 0, sellPath, address(this), block.timestamp
        ) {} catch {
            return (false, 10000); // HONEYPOT: bought fine, cannot sell
        }

        uint256 nativeBack = address(this).balance - nativeBefore;
        sellable = nativeBack > 0;
        lossBps = nativeBack >= buyNative ? 0 : ((buyNative - nativeBack) * 10000) / buyNative;
    }

    function test_checkPEPE() public {
        (bool sellable, uint256 lossBps) = _check(PEPE);
        emit log_named_string("token", "PEPE");
        emit log_named_string("verdict", sellable ? "SELLABLE (not a honeypot)" : "HONEYPOT / unsellable");
        emit log_named_uint("round-trip loss bps (fee+slippage+tax)", lossBps);
        assertTrue(sellable, "PEPE should be sellable on a fork");
    }

    IUniV2Factory constant FACTORY = IUniV2Factory(0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f);

    function test_catchHoneypot() public {
        // A scammer deploys a honeypot and seeds real liquidity (owner can move tokens freely).
        address scammer = makeAddr("scammer");
        vm.deal(scammer, 100 ether);
        vm.startPrank(scammer);
        HoneypotToken scam = new HoneypotToken();
        address pair = FACTORY.createPair(address(scam), WETH);
        scam.setPair(pair);
        scam.approve(address(ROUTER), type(uint256).max);
        ROUTER.addLiquidityETH{value: 10 ether}(address(scam), 1_000_000e18, 0, 0, scammer, block.timestamp);
        vm.stopPrank();

        // Now the tool runs its normal check as an ordinary buyer (NOT the owner).
        (bool sellable, uint256 lossBps) = _check(address(scam));
        emit log_named_string("token", "SCAM (deployed honeypot)");
        emit log_named_string("verdict", sellable ? "SELLABLE" : "HONEYPOT / unsellable -> FLAGGED RED");
        emit log_named_uint("loss bps", lossBps);
        assertFalse(sellable, "checker FAILED to catch the honeypot");
    }

    /// Live check driven by the scanner: reads CHECK_TOKEN env, forks latest, and prints a
    /// machine-parseable SCANRESULT line. No-op under a normal `forge test` (env unset).
    function test_checkEnvToken() public {
        address token = vm.envOr("CHECK_TOKEN", address(0));
        if (token == address(0)) return;
        // Per-chain params supplied by the scanner (default to Ethereum/Uniswap).
        string memory forkAlias = vm.envOr("CHECK_FORK", string("mainnet"));
        address routerAddr = vm.envOr("CHECK_ROUTER", address(ROUTER));
        address wnative = vm.envOr("CHECK_WNATIVE", WETH);
        vm.createSelectFork(forkAlias); // latest block on the target chain
        (bool sellable, uint256 lossBps) = _checkOn(token, routerAddr, wnative);
        emit log_named_string(
            "SCANRESULT",
            string.concat(vm.toString(token), " ", sellable ? "SELLABLE" : "HONEYPOT", " ", vm.toString(lossBps))
        );
    }

    receive() external payable {}
}

/// A textbook honeypot: anyone can BUY (receive from the pair), but only the owner can
/// SELL (send to the pair). Regular buyers are trapped — exactly what real ones do.
contract HoneypotToken {
    string public name = "ScamCoin";
    string public symbol = "SCAM";
    uint8 public decimals = 18;
    uint256 public totalSupply;
    address public owner;
    address public pair;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor() {
        owner = msg.sender;
        totalSupply = 1_000_000_000e18;
        balanceOf[msg.sender] = totalSupply;
        emit Transfer(address(0), msg.sender, totalSupply);
    }

    function setPair(address p) external {
        pair = p;
    }

    function approve(address s, uint256 a) external returns (bool) {
        allowance[msg.sender][s] = a;
        emit Approval(msg.sender, s, a);
        return true;
    }

    function transfer(address to, uint256 a) external returns (bool) {
        return _transfer(msg.sender, to, a);
    }

    function transferFrom(address f, address to, uint256 a) external returns (bool) {
        allowance[f][msg.sender] -= a;
        return _transfer(f, to, a);
    }

    function _transfer(address from, address to, uint256 a) internal returns (bool) {
        // THE TRAP: a transfer TO the pair (a sell) reverts unless it's the owner.
        require(to != pair || from == owner, "SCAM: cannot sell");
        balanceOf[from] -= a;
        balanceOf[to] += a;
        emit Transfer(from, to, a);
        return true;
    }
}
