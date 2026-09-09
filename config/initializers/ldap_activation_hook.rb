# frozen_string_literal: true

# Registers a synchronous LDAP/AD membership check as a before_create hook, from the outside,
# without touching app/models/user.rb.
#
# Intended to be dropped onto an already running production system (e.g. as a hotfix
# initializer): before a disabled Person account is persisted (see User#set_account_active /
# ENV['DEVISE_NEW_ACCOUNT_INACTIVE']), it is checked against a configured LDAP group.
# Members are activated immediately (account_active is flipped before the INSERT, so no
# separate update is needed); non-members, and registrations where the directory itself is
# unreachable, are rejected and the record is never created at all.
#
# Nested group membership is always resolved (see NESTED_GROUP_MATCHING_RULE below), which is
# an Active Directory-specific extensible match rule; against a non-AD directory that doesn't
# support it, only direct membership of LDAP_ACTIVATION_GROUP_DN will match.
#
# All LDAP settings are read from the environment; leave LDAP_HOST/LDAP_BASE/
# LDAP_ACTIVATION_GROUP_DN blank to disable the whole feature.
module LdapMembershipCheck
  # AD's LDAP_MATCHING_RULE_IN_CHAIN OID: makes the memberOf assertion below resolve nested
  # group membership (a member of a sub-group of LDAP_ACTIVATION_GROUP_DN counts too), not just
  # direct membership. Always applied, regardless of how the group DN itself is structured.
  NESTED_GROUP_MATCHING_RULE = '1.2.840.113556.1.4.1941'

  def self.enabled?
    ENV['LDAP_HOST'].present? && ENV['LDAP_BASE'].present? && ENV['LDAP_ACTIVATION_GROUP_DN'].present?
  end

  # @return [Boolean] whether uid exists in the directory and is a (possibly nested) member of
  #   LDAP_ACTIVATION_GROUP_DN. Raises Net::LDAP::Error if the directory itself is unreachable.
  def self.member?(uid)
    return false if uid.blank?

    filter = membership_filter(Net::LDAP::Filter.escape(uid))
    connection.search(base: ENV.fetch('LDAP_BASE', nil), filter: filter, attributes: ['dn']).present?
  end

  # Single-query alternative to calling .member? once per user (used by
  # LdapPeriodicRecheckJob): matches every (possibly nested) member of LDAP_ACTIVATION_GROUP_DN
  # at once, and returns the (downcased) LDAP_UID_ATTRIBUTE value of each as a Set. Raises
  # Net::LDAP::Error if the directory itself is unreachable.
  def self.members
    filter = membership_filter('*')
    entries = connection.search(base: ENV.fetch('LDAP_BASE', nil), filter: filter, attributes: [uid_attribute],
                                paged_searches: true)
    entries.to_a.filter_map { |entry| entry[uid_attribute]&.first&.downcase }.to_set
  end

  # uid_value is either an escaped uid (member?) or a literal '*' wildcard (members), matched
  # against LDAP_UID_ATTRIBUTE and combined with the nested-group membership assertion.
  def self.membership_filter(uid_value)
    group_dn = Net::LDAP::Filter.escape(ENV.fetch('LDAP_ACTIVATION_GROUP_DN', nil))
    Net::LDAP::Filter.construct(
      "(&(#{uid_attribute}=#{uid_value})(memberOf:#{NESTED_GROUP_MATCHING_RULE}:=#{group_dn}))",
    )
  end

  def self.uid_attribute
    ENV['LDAP_UID_ATTRIBUTE'].presence || 'sAMAccountName'
  end

  def self.connection
    Net::LDAP.new(
      host: ENV.fetch('LDAP_HOST', nil),
      port: ENV['LDAP_PORT'].presence || 389,
      base: ENV.fetch('LDAP_BASE', nil),
      encryption: encryption_options,
      auth: bind_auth,
    )
  end

  def self.bind_auth
    bind_dn = ENV.fetch('LDAP_BIND_DN', nil)
    return { method: :anonymous } if bind_dn.blank?

    { method: :simple, username: bind_dn, password: ENV.fetch('LDAP_BIND_PASSWORD', nil) }
  end

  def self.encryption_options
    case ENV['LDAP_ENCRYPTION_METHOD'].to_s
    when 'ssl' then { method: :simple_tls }
    when 'tls' then { method: :start_tls }
    end
  end
end

# `to_prepare` re-registers the callback every time the app is (re)loaded; guard against
# duplicate registration so repeated code reloads in development don't run the check twice.
Rails.application.config.to_prepare do
  next if User._create_callbacks.any? { |cb| cb.kind == :before && cb.filter == :ldap_activation_check }

  User.before_create(
    :ldap_activation_check,
    if: proc { |user| user.type == 'Person' && !user.account_active? && LdapMembershipCheck.enabled? },
  )

  User.send(:define_method, :ldap_activation_check) do
    account_attribute = ENV['LDAP_ACCOUNT_ATTRIBUTE'].presence || 'name_abbreviation'
    uid = public_send(account_attribute)

    if LdapMembershipCheck.member?(uid)
      self.account_active = true
    else
      errors.add(:base, 'is not a member of the required LDAP group')
      throw :abort
    end
  rescue Net::LDAP::Error => e
    # Block registration rather than silently creating a disabled account we can't verify.
    Rails.logger.error("LDAP group membership lookup failed for #{uid}: #{e.message}")
    errors.add(:base, 'Could not be verified against the LDAP directory, please try again later')
    throw :abort
  end
  User.send(:private, :ldap_activation_check)
end
