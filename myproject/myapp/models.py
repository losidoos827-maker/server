from django.db import models
from django.utils import timezone
from django.core.exceptions import ValidationError
import secrets


class SystemSetting(models.Model):
    """Global key-value configuration store for dynamic admin controls."""

    key = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Unique configuration identifier key."
    )
    value = models.CharField(
        max_length=255,
        default="",
        blank=True,
        help_text="Stored configuration value payload."
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Last configuration mutation timestamp."
    )

    class Meta:
        verbose_name = "System Setting"
        verbose_name_plural = "System Settings"

    def __str__(self):
        return f"Setting {self.key} = {self.value}"

    @classmethod
    def get_value(cls, key, default=""):
        try:
            obj = cls.objects.get(key=key)
            return obj.value
        except cls.DoesNotExist:
            return default


class SystemConfiguration(models.Model):
    """Singleton pattern configuration controlling game economic variables globally."""

    withdrawal_commission_percentage = models.PositiveIntegerField(
        default=5,
        help_text="Bonus cut percentage given to referrer upon an approved withdrawal."
    )
    platform_tax_percentage = models.PositiveIntegerField(
        default=15,
        help_text="Wager pool platform tax percentage collected from match pools."
    )
    winner_payout_percentage = models.PositiveIntegerField(
        default=85,
        help_text="Wager pool payout delivery payload percentage distributed to the winner."
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Global System Configuration"
        verbose_name_plural = "Global System Configuration"

    def clean(self):
        if self.platform_tax_percentage + self.winner_payout_percentage != 100:
            raise ValidationError(
                "The platform tax and winner payout percentages must equal exactly 100%."
            )
        if not 0 <= self.platform_tax_percentage <= 100:
            raise ValidationError("Platform tax must be between 0 and 100.")
        if not 0 <= self.winner_payout_percentage <= 100:
            raise ValidationError("Winner payout must be between 0 and 100.")
        if not 0 <= self.withdrawal_commission_percentage <= 100:
            raise ValidationError("Withdrawal commission must be between 0 and 100.")

    def save(self, *args, **kwargs):
        self.clean()
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        obj, created = cls.objects.get_or_create(
            pk=1,
            defaults={
                'withdrawal_commission_percentage': 5,
                'platform_tax_percentage': 15,
                'winner_payout_percentage': 85,
            },
        )
        return obj

    def __str__(self):
        return (
            f"System Matrix Rules "
            f"[Tax: {self.platform_tax_percentage}% | "
            f"Commission: {self.withdrawal_commission_percentage}%]"
        )


class UserProfileBalance(models.Model):
    nickname = models.CharField(
        max_length=100, blank=True, null=True,
        help_text="User's custom display name."
    )
    email = models.EmailField(
        blank=True, null=True, unique=True,
        help_text="User email."
    )
    profile_pic = models.ImageField(
        upload_to='profile_pics/', blank=True, null=True
    )

    device_token = models.CharField(max_length=255, unique=True, db_index=True)
    coins = models.IntegerField(default=0, help_text="Available active balance pool.")
    locked_coins = models.IntegerField(
        default=0,
        help_text="Escrowed coins held during active wagering matches."
    )
    referral_code = models.CharField(
        max_length=6, unique=True, db_index=True, blank=True
    )
    last_free_spin = models.BigIntegerField(
        default=0,
        help_text="Timestamp of last free spin in seconds"
    )

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.referral_code:
            while True:
                code = secrets.token_hex(3).upper()
                if not UserProfileBalance.objects.filter(referral_code=code).exists():
                    self.referral_code = code
                    break
        super().save(*args, **kwargs)

    def __str__(self):
        display_name = self.nickname if self.nickname else "Anonymous User"
        return (
            f"{display_name} | {self.device_token} "
            f"({self.referral_code}) - Coins: {self.coins}"
        )


class ReferralSystem(models.Model):
    """Immutable mapping tracking systemic invitation connections."""

    referrer = models.ForeignKey(
        UserProfileBalance, on_delete=models.CASCADE,
        related_name="referrals_initiated"
    )
    referred_user = models.ForeignKey(
        UserProfileBalance, on_delete=models.CASCADE,
        related_name="referred_by_link"
    )
    total_commission_earned = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('referred_user',)
        db_table = 'referral_system'

    def __str__(self):
        return (
            f"Referrer: {self.referrer.referral_code} ➡️ "
            f"Used By: {self.referred_user.referral_code}"
        )


class SystemTransactionLog(models.Model):
    """Explicit systemic auditing trace for compliance monitoring logs."""

    LOG_TYPES = [
        ('DEPOSIT', 'Manual Deposit Inflow'),
        ('WITHDRAWAL', 'Manual Withdrawal Outflow'),
        ('WAGER_ESCROW', 'Match Entry Lock'),
        ('WAGER_PAYOUT', 'Match Win Distribution'),
        ('WAGER_REFUND', 'Match Entry Refund'),
        ('REFERRAL_BONUS', 'Onboarding Sign-up Bonus'),
        ('REFERRAL_COMMISSION', 'Dynamic Withdrawal Bonus Reward'),
        ('SPIN_COST', 'Lucky Spin Paid Cost'),
        ('SPIN_REWARD', 'Lucky Spin Reward'),
    ]

    user_profile = models.ForeignKey(
        UserProfileBalance, on_delete=models.CASCADE,
        related_name="ledger_traces"
    )
    amount = models.IntegerField()
    log_type = models.CharField(max_length=25, choices=LOG_TYPES)
    reference_id = models.CharField(
        max_length=100, blank=True, null=True,
        help_text="Tracks match IDs or request tracking IDs."
    )
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"[{self.log_type}] {self.user_profile.device_token}: {self.amount}"


class GameRoom(models.Model):
    STATUS_CHOICES = [
        ('LOBBY', 'Lobby Waiting Frame'),
        ('ACTIVE', 'Active Wagering Match'),
        ('COMPLETED', 'Completed Ledger Payout'),
        ('CANCELLED', 'Rollback Cancelled Fail-Safe'),
    ]

    game_id = models.CharField(max_length=100, unique=True, db_index=True)
    bet_amount = models.IntegerField(
        default=0,
        help_text="Wager fee requirement per individual player profile."
    )
    total_pool_escrow = models.IntegerField(
        default=0,
        help_text="Total pooled contribution values in escrow."
    )
    service_fee_cut = models.IntegerField(
        default=0,
        help_text="System administration platform tax."
    )
    winner_payout = models.IntegerField(
        default=0,
        help_text="Delivery distribution payload sum."
    )
    game_status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='LOBBY'
    )

    # ---- Private / Friend Room Support ----
    is_private = models.BooleanField(
        default=False,
        help_text="True for friend-invite rooms with shareable code."
    )
    room_code = models.CharField(
        max_length=10, unique=True, null=True, blank=True,
        db_index=True,
        help_text="Short shareable code for private rooms."
    )

    # ---- NEW: Maximum players in this room (2 or 4) ----
    max_players = models.PositiveIntegerField(
        default=2,
        help_text="Maximum number of players allowed in this room (2 or 4)."
    )
    # ----------------------------------------------------

    players = models.ManyToManyField(
        UserProfileBalance, related_name='active_wager_rooms', blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if (
            self.game_status == 'ACTIVE'
            and (self.service_fee_cut + self.winner_payout != self.total_pool_escrow)
        ):
            raise ValidationError(
                "Accounting Error: Combined platform fee and payout "
                "value mismatch total escrow pools."
            )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"Match {self.game_id} [{self.game_status}] "
            f"Pool: {self.total_pool_escrow}"
        )


class SystemPaymentMethod(models.Model):
    METHOD_CHOICES = [
        ('JAZZCASH', 'JazzCash'),
        ('EASYPAISA', 'EasyPaisa'),
        ('BINANCE', 'Binance'),
    ]
    method_type = models.CharField(max_length=20, choices=METHOD_CHOICES, unique=True)
    account_name = models.CharField(max_length=100)
    account_number = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.get_method_type_display()} - {self.account_number}"


class DepositRequest(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending Verification'),
        ('APPROVED', 'Approved & Credited'),
        ('REJECTED', 'Rejected / Invalid'),
    ]
    device_token = models.CharField(max_length=255)
    amount = models.IntegerField()
    payment_method = models.CharField(max_length=50)
    sender_name = models.CharField(max_length=100, blank=True, null=True)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='PENDING')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.device_token} - {self.amount} ({self.status})"


class WithdrawalRequest(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    ]
    METHOD_CHOICES = [
        ('JAZZCASH', 'JazzCash'),
        ('EASYPAISA', 'EasyPaisa'),
        ('BINANCE', 'Binance'),
    ]
    device_token = models.CharField(max_length=150, db_index=True)
    amount = models.PositiveIntegerField()
    method = models.CharField(max_length=20, choices=METHOD_CHOICES)
    account_title = models.CharField(max_length=100)
    account_number = models.CharField(max_length=100)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='PENDING')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.method} Withdrawal ({self.amount} Coins) - {self.status}"


class GameUser(models.Model):
    username = models.CharField(max_length=50, unique=True)
    email = models.EmailField(unique=True)
    device_id = models.CharField(max_length=255, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    auth_token = models.CharField(max_length=512, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.username} - {self.email}"