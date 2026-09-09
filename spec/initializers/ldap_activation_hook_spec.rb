# frozen_string_literal: true

require 'rails_helper'

# Verifies the before_create hook registered from
# config/initializers/ldap_activation_hook.rb (not defined in app/models/user.rb) runs
# synchronously right before a disabled Person account is persisted.
# rubocop:disable RSpec/DescribeClass, RSpec/MultipleDescribes
describe 'LDAP activation hook' do
  # Person#account_active is forced by User#set_account_active based on this env var, so it
  # has to be set to actually get a disabled account out of the factory.
  def build_disabled_person(**attrs)
    stub_const('ENV', ENV.to_hash.merge('DEVISE_NEW_ACCOUNT_INACTIVE' => 'true'))
    build(:person, **attrs)
  end

  context 'when LDAP is not configured' do
    before { allow(LdapMembershipCheck).to receive(:enabled?).and_return(false) }

    it 'creates the user and leaves the account disabled' do
      user = build_disabled_person

      user.save!

      expect(user.reload.account_active).to be(false)
    end
  end

  context 'when the account is already active' do
    before { allow(LdapMembershipCheck).to receive(:enabled?).and_return(true) }

    it 'does not run the LDAP check' do
      user = build(:person)
      allow(LdapMembershipCheck).to receive(:member?)

      user.save!

      expect(LdapMembershipCheck).not_to have_received(:member?)
    end
  end

  context 'when LDAP is configured' do
    before { allow(LdapMembershipCheck).to receive(:enabled?).and_return(true) }

    it 'activates and persists the user when they are a member of the configured group' do
      user = build_disabled_person(name_abbreviation: 'jd')
      allow(LdapMembershipCheck).to receive(:member?).with('jd').and_return(true)

      user.save!

      expect(user.reload.account_active).to be(true)
    end

    it 'rejects (never persists) a user who is not a member of the configured group' do
      user = build_disabled_person(name_abbreviation: 'jd')
      allow(LdapMembershipCheck).to receive(:member?).with('jd').and_return(false)

      expect { user.save! }.to raise_error(ActiveRecord::RecordNotSaved)
      expect(User.unscoped.where(name_abbreviation: 'jd')).not_to exist
    end

    it 'rejects (never persists) a user when the LDAP connection raises' do
      user = build_disabled_person(name_abbreviation: 'jd')
      allow(LdapMembershipCheck).to receive(:member?).with('jd').and_raise(Net::LDAP::Error, 'connection refused')

      expect { user.save! }.to raise_error(ActiveRecord::RecordNotSaved)
      expect(User.unscoped.where(name_abbreviation: 'jd')).not_to exist
    end
  end
end
# rubocop:enable RSpec/DescribeClass

describe LdapMembershipCheck do
  describe '.enabled?' do
    it 'is true when host, base and activation filter are present' do
      stub_const('ENV', ENV.to_hash.merge(
                          'LDAP_HOST' => 'ldap.example.org',
                          'LDAP_BASE' => 'dc=example,dc=org',
                          'LDAP_ACTIVATION_FILTER' => '(sAMAccountName=%<uid>s)',
                        ))

      expect(described_class.enabled?).to be(true)
    end

    it 'is false when the activation filter is missing' do
      stub_const('ENV', ENV.to_hash.merge(
                          'LDAP_HOST' => 'ldap.example.org',
                          'LDAP_BASE' => 'dc=example,dc=org',
                          'LDAP_ACTIVATION_FILTER' => nil,
                        ))

      expect(described_class.enabled?).to be(false)
    end
  end

  describe '.member?' do
    let(:connection) { instance_double(Net::LDAP) }

    before do
      stub_const('ENV', ENV.to_hash.merge(
                          'LDAP_HOST' => 'ldap.example.org',
                          'LDAP_BASE' => 'dc=example,dc=org',
                          'LDAP_ACTIVATION_FILTER' =>
                            '(&(sAMAccountName=%<uid>s)(memberOf:1.2.840.113556.1.4.1941:=' \
                            'cn=eln-users,ou=groups,dc=example,dc=org))',
                        ))
      allow(Net::LDAP).to receive(:new).and_return(connection)
    end

    it 'substitutes the escaped uid into the configured filter and returns true on a match' do
      allow(connection).to receive(:search).and_return([{ dn: 'cn=jdoe,dc=example,dc=org' }])

      expect(described_class.member?('jdoe')).to be(true)
      expect(connection).to have_received(:search).with(
        base: 'dc=example,dc=org',
        filter: Net::LDAP::Filter.construct(
          '(&(sAMAccountName=jdoe)(memberOf:1.2.840.113556.1.4.1941:=cn=eln-users,ou=groups,dc=example,dc=org))',
        ),
        attributes: ['dn'],
      )
    end

    it 'returns false when the filter matches no entry' do
      allow(connection).to receive(:search).and_return([])

      expect(described_class.member?('jdoe')).to be(false)
    end

    it 'escapes LDAP special characters in uid to prevent filter injection' do
      allow(connection).to receive(:search).and_return([])

      described_class.member?('jdoe)(uid=*')

      expect(connection).to have_received(:search).with(
        hash_including(filter: Net::LDAP::Filter.construct(
          '(&(sAMAccountName=jdoe\28\29\28uid=\2a)(memberOf:1.2.840.113556.1.4.1941:=' \
          'cn=eln-users,ou=groups,dc=example,dc=org))',
        )),
      )
    end

    it 'returns false without querying when uid is blank' do
      expect(described_class.member?('')).to be(false)
      expect(connection).not_to have_received(:search)
    end
  end
end
# rubocop:enable RSpec/MultipleDescribes
