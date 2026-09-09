# frozen_string_literal: true

require 'rails_helper'

# Verifies the hook registered from config/initializers/ldap_activation_hook.rb (not defined in
# app/models/user.rb) fires right after a disabled Person account is created, and that
# LdapActivationJob (also defined there) resolves the account correctly.
# rubocop:disable RSpec/DescribeClass
describe 'LDAP activation hook', :active_job do
  # Person#account_active is forced by User#set_account_active based on this env var, so it
  # has to be set to actually get a disabled account out of the factory.
  def build_disabled_person(**attrs)
    stub_const('ENV', ENV.to_hash.merge('DEVISE_NEW_ACCOUNT_INACTIVE' => 'true'))
    build(:person, **attrs)
  end

  def create_disabled_person(**attrs)
    stub_const('ENV', ENV.to_hash.merge('DEVISE_NEW_ACCOUNT_INACTIVE' => 'true'))
    create(:person, **attrs)
  end

  describe 'hook registration' do
    context 'when LDAP is configured' do
      before { allow(LdapActivationJob).to receive(:enabled?).and_return(true) }

      it 'queues LdapActivationJob for a freshly created, disabled Person' do
        user = build_disabled_person

        expect { user.save! }.to have_enqueued_job(LdapActivationJob)
        expect(enqueued_jobs.last[:args]).to eq([user.id])
      end
    end

    context 'when LDAP is not configured' do
      before { allow(LdapActivationJob).to receive(:enabled?).and_return(false) }

      it 'does not queue LdapActivationJob' do
        user = build_disabled_person

        expect { user.save! }.not_to have_enqueued_job(LdapActivationJob)
      end
    end

    context 'when the account is already active' do
      before { allow(LdapActivationJob).to receive(:enabled?).and_return(true) }

      it 'does not queue LdapActivationJob' do
        user = build(:person)

        expect { user.save! }.not_to have_enqueued_job(LdapActivationJob)
      end
    end
  end

  describe LdapActivationJob do
    context 'when LDAP is not configured' do
      before { allow(described_class).to receive(:enabled?).and_return(false) }

      it 'does not touch the user' do
        user = create_disabled_person(name_abbreviation: 'jd')

        described_class.new.perform(user.id)

        expect(user.reload.account_active).to be(false)
      end
    end

    context 'when LDAP is configured' do
      let(:connection) { instance_double(Net::LDAP) }

      before do
        stub_const('ENV', ENV.to_hash.merge(
                            'LDAP_HOST' => 'ldap.example.org',
                            'LDAP_BASE' => 'dc=example,dc=org',
                            'LDAP_ACTIVATION_GROUP_DN' => 'cn=eln-users,ou=groups,dc=example,dc=org',
                          ))
        allow(Net::LDAP).to receive(:new).and_return(connection)
      end

      it 'activates the user when they are a member of the configured group' do
        user = create_disabled_person(name_abbreviation: 'jd')
        allow(connection).to receive(:search).and_return(
          [{ memberof: ['cn=eln-users,ou=groups,dc=example,dc=org'] }],
        )

        described_class.new.perform(user.id)

        expect(user.reload.account_active).to be(true)
      end

      it 'deletes the user when they are not a member of the configured group' do
        user = create_disabled_person(name_abbreviation: 'jd')
        allow(connection).to receive(:search).and_return([{ memberof: ['cn=other,ou=groups,dc=example,dc=org'] }])

        described_class.new.perform(user.id)

        expect(User.unscoped.find(user.id).deleted_at).to be_present
      end

      it 'deletes the user when no directory entry is found' do
        user = create_disabled_person(name_abbreviation: 'jd')
        allow(connection).to receive(:search).and_return([])

        described_class.new.perform(user.id)

        expect(User.unscoped.find(user.id).deleted_at).to be_present
      end

      it 'does nothing when the user is already active' do
        user = create(:person, account_active: true, name_abbreviation: 'atv')
        allow(connection).to receive(:search)

        described_class.new.perform(user.id)

        expect(connection).not_to have_received(:search)
        expect(user.reload.account_active).to be(true)
      end

      it 'does nothing when the user no longer exists' do
        expect { described_class.new.perform(-1) }.not_to raise_error
      end

      it 'leaves the user disabled when the LDAP connection raises' do
        user = create_disabled_person(name_abbreviation: 'jd')
        allow(connection).to receive(:search).and_raise(Net::LDAP::Error, 'connection refused')

        described_class.new.perform(user.id)

        expect(User.unscoped.find(user.id).deleted_at).to be_blank
        expect(user.reload.account_active).to be(false)
      end
    end
  end
end
# rubocop:enable RSpec/DescribeClass
